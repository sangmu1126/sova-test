"""Snapshot + CDC by PostgreSQL logical replication: the conformance kit and
the handoff through the real control plane.

The data moves before the fence; inside it only the changes still in flight
are applied, confirmed on the same slot that carried them (§16.2).
"""

from datetime import UTC, datetime, timedelta
import time

from adapter_conformance import HandoffAdapterConformance, apply, readback
from handoff_drive import without_transport_control
from qualification_fixture_placement import run_local_handoff as run_handoff
from postgres_business import (
    SCHEMA,
    TABLES,
    logical_replication_available,
    replication_conninfo,
)
from postgres_world import PostgresWorld
from psycopg import sql
import pytest

from sova.adapters.postgres_logical_replication import PostgresLogicalReplicationAdapter
from sova.domain.contracts import EffectStatus
from sova.domain.handoff_authority import HandoffState

pytestmark = pytest.mark.postgres


@pytest.fixture
def logical(business):
    if not logical_replication_available(business["connect"], business["names"]["src"]):
        pytest.skip("the PostgreSQL test server runs without wal_level=logical")
    return business


class LogicalReplicationWorld(PostgresWorld):
    """The PostgreSQL world with the logical-replication adapter. Business
    writes settle - the subscription confirms them on its slot - before the
    kit looks again, so an observation never races the apply worker."""

    def _adapter(self, source, target, **overrides):
        return PostgresLogicalReplicationAdapter(
            "postgres-logical-replication", connect=self.b["connect"],
            source_db=self._db(source), target_db=self._db(target), app_role=self.b["app"],
            tables=TABLES, schema_sql=SCHEMA, route=self._route,
            writer_roles=tuple(self.writers.values()), session_wait_seconds=2.0,
            source_conninfo=replication_conninfo(self._db(source)), **overrides)

    def settle(self, seconds=30):
        adapter = self.adapter()
        if adapter._subscription() is None:
            return
        with self.b["connect"](self._db("source")) as conn:
            position = conn.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0]
        deadline = time.monotonic() + seconds
        while not adapter._caught_up(position):
            assert time.monotonic() < deadline, "the subscription did not catch up"
            time.sleep(0.05)

    def business_write(self, count):
        super().business_write(count)
        self.settle()

    def business_delete(self, count):
        super().business_delete(count)
        self.settle()

    def release(self):
        for adapter in (self.adapter(), self.reverse_adapter()):
            adapter.release_replication()


@pytest.fixture
def cdc_world(logical):
    world = LogicalReplicationWorld(logical)
    yield world
    world.release()  # never leave a slot retaining WAL on the test server


class TestPostgresLogicalReplicationConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, cdc_world):
        return cdc_world


def table_rows(business, side, query):
    with business["connect"](business["names"][side]) as conn:
        return conn.execute(query).fetchall()


def test_changes_flow_before_the_fence_and_nothing_is_left_after(cdc_world, logical):
    """Writes and deletes made while the source still serves reach the target
    before admission is paused; after the final changes no publication, slot
    or subscription remains, and the order numbers continue."""
    world, adapter = cdc_world, cdc_world.adapter()
    for action in ("stage_target", "start_sync"):
        apply(adapter, action)
    world.business_write(4)
    world.business_delete(2)
    count = "SELECT count(*) FROM orders"
    assert table_rows(logical, "tgt", count) == table_rows(logical, "src", count)
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes"):
        apply(world.adapter(), action)
        assert readback(world.adapter(), action) == EffectStatus.APPLIED, action
    released, detail = world.adapter().replication_released()
    assert released, detail
    sequence = "SELECT last_value FROM order_number_seq"
    assert table_rows(logical, "tgt", sequence) == table_rows(logical, "src", sequence)


def test_a_table_that_cannot_publish_deletes_is_refused_untouched(cdc_world, logical):
    """Publishing a table without a replica identity would make the
    application's UPDATE and DELETE on the source fail. Nothing is created;
    the readback names the table and the business goes on."""
    names, app = logical["names"], logical["app"]
    for name in (names["src"], names["tgt"]):
        with logical["connect"](name) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS notes (body text)")
    with logical["connect"](names["src"]) as conn:
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON notes TO {}").format(
            sql.Identifier(app)))
    world = cdc_world
    adapter = PostgresLogicalReplicationAdapter(
        "postgres-logical-replication", connect=logical["connect"], source_db=names["src"],
        target_db=names["tgt"], app_role=app, tables=TABLES + (("notes", "body"),),
        schema_sql=SCHEMA, route=world._route, writer_roles=(app, logical["batch"]),
        source_conninfo=replication_conninfo(names["src"]))
    apply(adapter, "start_sync")
    assert readback(adapter, "start_sync") == EffectStatus.UNKNOWN
    holds, detail = adapter._check_start_sync()
    assert not holds and detail["tables_without_replica_identity"] == ["notes"]
    assert adapter.replication_released()[0]  # nothing was created
    with logical["connect_as"](names["src"], app) as conn:
        conn.execute("INSERT INTO notes VALUES ('still works')")
        conn.execute("DELETE FROM notes")


def test_unconfirmed_final_changes_keep_the_replication_for_repair(cdc_world, logical):
    """The subscription stops before the fence (a broken apply worker): the
    final changes cannot be confirmed in time, nothing is removed, and the
    readback stays UNKNOWN. Once repaired, the same action completes."""
    world = cdc_world
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    with logical["connect"](logical["names"]["tgt"]) as conn:
        conn.autocommit = True
        conn.execute(sql.SQL("ALTER SUBSCRIPTION {} DISABLE").format(
            sql.Identifier(world.adapter().name)))
    super(LogicalReplicationWorld, world).business_write(3)  # not replicated now
    for action in ("pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    hurried = world._adapter("source", "target", final_sync_wait_seconds=1.0)
    apply(hurried, "apply_final_changes")
    assert readback(hurried, "apply_final_changes") == EffectStatus.UNKNOWN
    assert hurried._subscription() is not None and hurried._slot() is not None
    with logical["connect"](logical["names"]["tgt"]) as conn:
        conn.autocommit = True
        conn.execute(sql.SQL("ALTER SUBSCRIPTION {} ENABLE").format(
            sql.Identifier(world.adapter().name)))
    apply(world.adapter(), "apply_final_changes")
    assert readback(world.adapter(), "apply_final_changes") == EffectStatus.APPLIED


def test_an_aborted_handoff_releases_the_slot(cdc_world, logical):
    """After start_sync the source keeps WAL for the slot; an abort must
    release it, and the release is read back from the servers."""
    world = cdc_world
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    assert not world.adapter().replication_released()[0]
    world.adapter().release_replication()
    released, detail = world.adapter().replication_released()
    assert released, detail
    assert world.can_write("source", "app")  # the source was never paused


def test_a_logical_replication_handoff_completes_through_the_control_plane(
        postgres_database, logical):
    names, app = logical["names"], logical["app"]

    def write_during_sync(step):
        if step == "pause_admission":  # after the initial copy, before the fence
            with logical["connect_as"](names["src"], app) as conn:
                conn.execute("INSERT INTO orders VALUES (77, 'late', 'key-77', 7)")
                conn.execute("INSERT INTO idempotency_results VALUES "
                             "('late', 'key-77', 'confirmed')")

    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), logical,
                    adjust=without_transport_control, strategy="snapshot-cdc",
                    before=write_during_sync, cdc=True)
    try:
        assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (
            x["record"].reason, x["record"].held_from, x["observed"].get("uncertain"),
            x["observed"].get("failed"))
        assert table_rows(logical, "tgt", "SELECT count(*) FROM orders WHERE id=77") == [(1,)]
        for table, _ in TABLES:
            assert x["probes"].content_equal(table)[0], table
        assert x["adapter"].replication_released()[0]
        assert x["probes"].single_writer()[0]
    finally:
        x["adapter"].release_replication()
