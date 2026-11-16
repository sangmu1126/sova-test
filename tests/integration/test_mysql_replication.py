"""MySQL change capture between two servers against the conformance kit, and
what replication adds: a write stop that no longer carries the bulk of the
data, counters that do not hand out a withdrawn number again, and a channel
that is removed on an abort instead of writing into the target forever."""

import time

from adapter_conformance import HandoffAdapterConformance, apply, readback
import pytest
from mysql_pair_world import MySqlPairWorld

from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.mysql, pytest.mark.kubernetes]


@pytest.fixture
def world(mysql_pair_business):
    return MySqlPairWorld(mysql_pair_business)


class TestMySqlReplicationConformance(HandoffAdapterConformance):
    pass


def bulk(world, rows):
    with world.b["connect_as"](world.names["src"], world.b["app"]) as conn, \
            conn.cursor() as cur:
        cur.executemany("INSERT INTO work_items VALUES (%s, %s, 'queued')",
                        [(1000 + i, i) for i in range(rows)])
        conn.commit()


def inserted_on_target(world):
    with world.b["connect"](world.names["tgt"]) as conn, conn.cursor() as cur:
        cur.execute("SHOW GLOBAL STATUS LIKE 'Innodb_rows_inserted'")
        return int(cur.fetchone()[1])


def window(world, steps):
    """(seconds, rows the target server wrote) from the pause to the verified state."""
    rows, started = inserted_on_target(world), time.monotonic()
    for action in ("pause_admission", "fence_source_writes", *steps, "verify_final_state"):
        apply(world.adapter(), action)
    return time.monotonic() - started, inserted_on_target(world) - rows


def test_the_write_stop_carries_only_what_changed_since_the_sync(world):
    bulk(world, 20000)
    apply(world.adapter(), "stage_target")
    started = time.monotonic()
    apply(world.adapter(), "start_sync")
    synced = time.monotonic() - started
    assert readback(world.adapter(), "start_sync") == EffectStatus.APPLIED
    world.business_write(5)  # the source still serves; replication carries it
    world.business_delete(2)
    spent, rows = window(world, ("apply_final_changes",))
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    assert world.content("source") == world.content("target") and world.channel() == []
    assert rows < 100, rows  # the bulk was not copied again inside the stop
    print(f"change capture: initial sync {synced:.1f}s; stop {spent:.1f}s, "
          f"{rows} rows written in it")


def test_an_offline_copy_writes_everything_inside_the_stop(world):
    """The same business without a sync: the contrast the template choice is about."""
    bulk(world, 20000)
    apply(world.adapter(), "stage_target")
    spent, rows = window(world, ("copy_content",))
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    assert rows >= 20000, rows
    print(f"offline copy: stop {spent:.1f}s, {rows} rows written in it")


def test_a_number_withdrawn_after_the_sync_is_not_issued_again(world):
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    with world.b["connect_as"](world.names["src"], world.b["app"]) as conn, \
            conn.cursor() as cur:
        cur.execute("INSERT INTO order_numbers () VALUES ()")  # 11, replicated
        conn.commit()
        cur.execute("INSERT INTO order_numbers () VALUES ()")  # 12: handed out, then
        conn.rollback()  # rolled back - InnoDB keeps it used, the binary log never sees it
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes",
                   "activate_target"):
        apply(world.adapter(), action)
    with world.b["connect_as"](world.names["tgt"], world.b["app"]) as conn, \
            conn.cursor() as cur:
        cur.execute("INSERT INTO order_numbers () VALUES ()")
        cur.execute("SELECT max(n) FROM order_numbers")
        assert cur.fetchone()[0] == 13
        conn.commit()


def test_an_aborted_sync_removes_its_channel(world):
    adapter = world.adapter()
    for action in ("stage_target", "start_sync"):
        apply(adapter, action)
    assert len(world.channel()) == 1 and not adapter.replication_released()
    adapter.release_replication()
    assert adapter.replication_released() and world.channel() == []
    world.business_write(3)  # nothing reaches the target any more
    assert world.content("source") != world.content("target")
