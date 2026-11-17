"""MySQL change capture from a source that grants no global read lock, as
a source account may not (managed services commonly withhold it): the
source account here has every privilege but RELOAD and FLUSH_TABLES. The lockless snapshot carries the writes made while it
is taken, the channel goes back to strict application once past the copy's
changes, and a table without a primary key is refused."""

from datetime import UTC, datetime
import threading
import time

from adapter_conformance import apply, command, readback
from mysql_business import SCHEMA, TABLES
from mysql_pair_business import REPLICATION
from mysql_pair_world import MySqlPairWorld
import pytest

from sova.adapters.mysql_replication import MySqlReplicationAdapter
from sova.domain.contracts import ContractError, EffectStatus

pytestmark = [pytest.mark.mysql, pytest.mark.kubernetes]
NO_LOCK = ("sova_nolock", "sova-no-reload-test-secret")


@pytest.fixture
def no_lock_connect(mysql_pair_business):
    """Administrative connections whose source account cannot take the global
    read lock (every privilege but RELOAD and FLUSH_TABLES)."""
    b = mysql_pair_business
    with b["connect"](None) as conn, conn.cursor() as cur:
        cur.execute(f"CREATE USER IF NOT EXISTS '{NO_LOCK[0]}'@'%' IDENTIFIED BY '{NO_LOCK[1]}'")
        cur.execute(f"GRANT ALL ON *.* TO '{NO_LOCK[0]}'@'%' WITH GRANT OPTION")
        # The global read lock needs RELOAD or, since 8.0.31, FLUSH_TABLES.
        cur.execute(f"REVOKE RELOAD, FLUSH_TABLES ON *.* FROM '{NO_LOCK[0]}'@'%'")
        conn.commit()

    def connect(database):
        side = b["side_of"](database)
        settings = b["admins"][side]
        if side == "source":
            settings = {**settings, "user": NO_LOCK[0], "password": NO_LOCK[1]}
        return b["pymysql"].connect(**settings, autocommit=False)

    yield connect
    with b["connect"](None) as conn, conn.cursor() as cur:
        cur.execute(f"DROP USER IF EXISTS '{NO_LOCK[0]}'@'%'")
        conn.commit()


def adapter(world, connect, snapshot, tables=TABLES):
    return MySqlReplicationAdapter(
        "mysql-replication", connect=connect, source_db=world.names["src"],
        target_db=world.names["tgt"], app_account=world.b["app"], tables=tables,
        schema_sql=SCHEMA, route=world._route, writer_accounts=tuple(world.writers.values()),
        session_wait_seconds=2.0, source_endpoint=world.b["source_endpoint"],
        replication_account=f"{REPLICATION[0]}@%", replication_password=REPLICATION[1],
        catch_up_seconds=60, snapshot=snapshot)


def exec_mode(world):
    return applier(world)[0]


def applier(world):
    """The target server's (replica_exec_mode, replica_parallel_workers)."""
    with world.b["connect"](world.names["tgt"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT @@GLOBAL.replica_exec_mode, @@GLOBAL.replica_parallel_workers")
        return cur.fetchone()


def test_the_global_read_lock_is_what_the_source_refuses(mysql_pair_business,
                                                          no_lock_connect):
    world = MySqlPairWorld(mysql_pair_business)
    locked = adapter(world, no_lock_connect, "locked")
    apply(locked, "stage_target")
    with pytest.raises(mysql_pair_business["pymysql"].err.OperationalError) as refused:
        apply(locked, "start_sync")
    assert refused.value.args[0] == 1227, refused.value  # RELOAD or FLUSH_TABLES needed


def test_a_lockless_sync_carries_the_writes_made_while_it_is_taken(mysql_pair_business,
                                                                     no_lock_connect):
    b = mysql_pair_business
    world = MySqlPairWorld(b)
    lockless = adapter(world, no_lock_connect, "lockless")
    apply(lockless, "stage_target")
    before = applier(world)  # the operator's own settings, put back after the window
    stop, done = threading.Event(), {"writes": 0}

    def business():  # inserts, updates and deletes through the snapshot's window
        with b["connect_as"](b["names"]["src"], b["app"]) as conn, conn.cursor() as cur:
            i = 0
            while not stop.is_set():
                cur.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                            (10000 + i, "live", f"live-{i}", i))
                cur.execute("UPDATE orders SET amount = amount + 1 WHERE id = %s",
                            (1 + i % 50,))
                cur.execute("DELETE FROM work_items WHERE id = %s", (1 + (i * 7) % 50,))
                conn.commit()
                done["writes"] += 3
                i += 1

    writer = threading.Thread(target=business)
    writer.start()
    try:
        time.sleep(0.3)
        apply(lockless, "start_sync")
        time.sleep(0.3)
    finally:
        stop.set()
        writer.join(30)
    observation = lockless.readback(command(lockless.adapter_id, "start_sync"),
                                    datetime.now(UTC))
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    assert observation.evidence["replay_window_open"] is False
    assert applier(world) == before == ("STRICT", observation.evidence["applier_threads"])
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes"):
        apply(lockless, action)
    assert readback(lockless, "verify_final_state") == EffectStatus.APPLIED
    assert world.content("source") == world.content("target")
    print(f"writes while syncing: {done['writes']}")


def test_a_table_without_a_primary_key_is_refused(mysql_pair_business, no_lock_connect):
    b = mysql_pair_business
    world = MySqlPairWorld(b)
    with b["connect"](b["names"]["src"]) as conn, conn.cursor() as cur:
        cur.execute(f"CREATE TABLE `{b['names']['src']}`.audit_log (id BIGINT NOT NULL, "
                    "note VARCHAR(64))")
        conn.commit()
    lockless = adapter(world, no_lock_connect, "lockless",
                       tables=(*TABLES, ("audit_log", "id")))
    with pytest.raises(ContractError, match="primary key"):
        lockless._start_sync()
    assert exec_mode(world) == "STRICT"
