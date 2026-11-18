"""Real PostgreSQL commit windows; no cloud or adapter effect is executed."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Event
import time

from handoff_support import admitted_handoff
import psycopg
import pytest
from psycopg.types.json import Jsonb
from witness_support import MemoryAuthorityWitness

from sova.adapters.postgres_handoff_authority import PostgresHandoffAuthority
from sova.domain.handoff_authority import AuthorityRollbackDetected

pytestmark = pytest.mark.postgres


class PausedAppend(MemoryAuthorityWitness):
    def __init__(self):
        super().__init__()
        self.pause = False
        self.appended, self.release = Event(), Event()

    def append(self, *args):
        super().append(*args)
        if self.pause:
            self.appended.set()
            if not self.release.wait(10):
                raise RuntimeError("Commit-window test did not release its writer")


@pytest.mark.parametrize("first_open", [False, True], ids=["existing-row", "missing-row"])
def test_reader_waits_for_witnessed_but_uncommitted_authority(postgres_database, now, first_open):
    witness = PausedAppend()
    h = admitted_handoff(postgres_database, now, witness_for=lambda tenant: witness)
    before = None if first_open else h["coordinator"].open(h["revision"], h["plan"], actor_id="writer")
    unit = json.loads(h["revision"].canonical)["unit"]["unit_digest"]
    connected, pid = Event(), []

    def reader_connect():
        conn = h["connect"]()
        pid.append(conn.info.backend_pid)
        connected.set()
        return conn

    reader = PostgresHandoffAuthority(reader_connect, tenant_id=h["tenant"], witness=witness)
    witness.pause = True

    def write():
        if first_open:
            return h["coordinator"].open(h["revision"], h["plan"], actor_id="writer")
        after = replace(before, revision=before.revision + 1, reason="CONCURRENCY_TEST")
        return h["store"].transition(before, after, action="test-transition", actor_id="writer")

    with ThreadPoolExecutor(2) as pool:
        writer = pool.submit(write)
        future = None
        try:
            assert witness.appended.wait(10), "Writer never reached the real pre-commit window"
            future = pool.submit(reader.current, h["app"], unit)
            assert connected.wait(5)
            locked = False
            with psycopg.connect(postgres_database[0], autocommit=True) as admin:
                until = time.monotonic() + 3
                while not future.done() and time.monotonic() < until:
                    row = admin.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid[0],)).fetchone()
                    if row == ("Lock",):
                        locked = True
                        break
                    time.sleep(.01)
            assert locked and not future.done(), "Read compared a pre-commit row with an ahead-of-commit witness"
        finally:
            witness.release.set()
            after = writer.result(timeout=10)
        assert future.result(timeout=10) == after


@pytest.mark.parametrize("sql_rollback", [False, True], ids=["restored-row", "failed-commit"])
def test_actual_rollback_still_blocks_until_explicit_recovery(postgres_database, now, sql_rollback):
    witness = MemoryAuthorityWitness()
    h = admitted_handoff(postgres_database, now, witness_for=lambda tenant: witness)
    before = h["coordinator"].open(h["revision"], h["plan"], actor_id="writer")
    after = replace(before, revision=before.revision + 1, reason="CONCURRENCY_TEST")
    if sql_rollback:
        original = witness.append
        def fail_after_append(*args):
            original(*args)
            raise RuntimeError("Deliberately roll SQL back after the durable witness append")
        witness.append = fail_after_append
        try:
            with pytest.raises(RuntimeError, match="Deliberately"):
                h["store"].transition(before, after, action="test-transition", actor_id="writer")
        finally:
            witness.append = original
    else:
        h["store"].transition(before, after, action="test-transition", actor_id="writer")
        # Isolated test administrator simulates an actual authority row restore.
        with psycopg.connect(postgres_database[0]) as admin:
            admin.execute("UPDATE sova_control.handoff_authority SET revision=%s,document=%s "
                          "WHERE tenant_id=%s AND application_id=%s AND unit_digest=%s",
                          (before.revision, Jsonb(before.document), h["tenant"], h["app"], before.unit_digest))
    with pytest.raises(AuthorityRollbackDetected):
        h["store"].current(h["app"], before.unit_digest)
    with pytest.raises(AuthorityRollbackDetected):
        h["store"].transition(before, after, action="test-transition", actor_id="writer")
    recovered = h["store"].recover(h["app"], before.unit_digest, h["revision"], actor_id="recovery-operator")
    assert recovered.epoch == after.epoch + 1 and recovered.revision == after.revision + 1
    assert h["store"].current(h["app"], before.unit_digest) == recovered
