"""Real polling processes with SIGTERM/SIGKILL, HTTP orders and PostgreSQL.

Shared file route and after-apply barrier are explicit fixtures. No Kubernetes
quiescence is fabricated and lease expiry alone never permits recovery.
"""

from contextlib import contextmanager
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from qualification_orders import application, observe, serve
from qualification_runtime import runtime_service_journey
from qualification_worker import FixtureRoute, WorkerProcess
from test_order_http_handoff import http_business as http_business  # noqa: F401

from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.qualification.http_orders import OrderHttpClient
from sova.qualification.requests import OrderIntent, RequestJournal, reconcile

pytestmark = pytest.mark.postgres


def execution(h):
    repo = PostgresLifecycleRepository(h.connect, tenant_id=h.tenant)
    with repo.transaction() as conn:
        row = conn.execute("SELECT owner_id,epoch,mode,valid_until<=clock_timestamp() "
                           "FROM sova_control.app_execution WHERE application_id=%s", (h.app,)).fetchone()
        effects = conn.execute("SELECT fingerprint,status,command_document->>'action' "
                               "FROM sova_control.effects WHERE application_id=%s ORDER BY fingerprint",
                               (h.app,)).fetchall()
    return {"owner": row[0], "epoch": row[1], "mode": row[2], "lease_expired": row[3],
            "effects": [{"digest": r[0], "status": r[1], "action": r[2]} for r in effects]}


@contextmanager
def orders(h):
    profile = Path(__file__).resolve().parents[2] / "examples/qualification/orders-http-postgres-v1.json"
    journal = RequestJournal.create(h.directory / "client.sqlite", run_id="worker-" + uuid4().hex,
                                    tenant_id=h.tenant, profile_digest=hashlib.sha256(profile.read_bytes()).hexdigest())
    route = FixtureRoute(h.directory / "route.json", scope={"tenant": h.tenant, "application": h.app,
                         "run": journal.scope["run_id"]}, databases=(h.business["names"]["src"], h.business["names"]["tgt"]))
    route.set(h.business["names"]["src"])
    h.route = route
    try:
        token = uuid4().hex
        with serve(application(h.business, route, journal.scope, token)) as origin:
            client = OrderHttpClient(origin, bearer_token=token, journal=journal)
            try:
                yield journal, client
            finally:
                client.close()
    finally:
        journal.close()


def applied_copy_once(events):
    started = [e for e in events if e["event"] == "apply_started" and e["action"] == "copy_content"]
    committed = [e for e in events if e["event"] == "apply_committed" and e["action"] == "copy_content"]
    assert len(started) == len(committed) == 1
    assert started[0]["command_digest"] == committed[0]["command_digest"]


def test_polling_worker_finishes_current_effect_on_sigterm_and_successor_continues(
        postgres_database, http_business, tmp_path):
    with runtime_service_journey(postgres_database, http_business, tmp_path,
                                  case_name="worker-graceful") as h:
        compiled = h.register_forward()
        unit = h.approve_and_admit(compiled, "forward-approval")["unit_digest"]
        with orders(h) as (journal, client):
            before = OrderIntent("before", "worker-customer", "before", "10")
            retry = OrderIntent("fenced", "worker-customer", "fenced", "20")
            after = OrderIntent("after", "worker-customer", "after", "30")
            assert client.submit(before) == 200
            a = WorkerProcess(h, label="worker-a", pause_after="copy_content")
            b = None
            try:
                a.wait(a.ready.exists)
                assert client.submit(retry) == 503
                held = execution(h)
                assert held["owner"] == a.owner and not held["lease_expired"]
                assert any(e["action"] == "copy_content" and e["status"] == "in_flight" for e in held["effects"])
                b = WorkerProcess(h, label="worker-b")
                b.wait(lambda: any(e["event"] == "drive_returned" and e["owner"] == b.owner and e["state"] is None
                                   for e in b.events()))
                assert execution(h)["owner"] == a.owner
                a_result = a.stop()  # signal first, then release the current effect's barrier
                b.wait(lambda: h.status(unit)["state"] == "HANDOFF_COMPLETE")
                b_result = b.stop()
                assert h.route.get() == http_business["names"]["tgt"]
                assert client.submit(retry) == 200
                assert client.submit(after) == 200
                observed = observe(http_business, h.route.get(), journal)
                result = reconcile(journal.snapshot(), observed)
                assert result["data_integrity"] == "SATISFIED"
                assert result["counts"]["confirmed_requests"] == 3
                assert result["counts"]["submission_attempts"] == 4
                assert result["counts"]["http_error_attempts"] == 1
                events = a.events()
                applied_copy_once(events)
                final = execution(h)
                assert not any(e["status"] in {"prepared", "in_flight", "unknown"} for e in final["effects"])
                assert final["epoch"] > held["epoch"]
                h.record("worker-graceful", authority=h.status(unit), before_signal=held, after_signal=final,
                         worker_processes=[a_result, b_result], trace=events,
                         client_journal=journal.snapshot(), independent_observation=observed, measurement=result,
                         route="shared file fixture read by real HTTP order app", worker_cli_deployed=False)
            finally:
                a.stop()
                if b:
                    b.stop()


def test_sigkill_after_commit_keeps_recovery_barrier_without_a_quiescence_proof(
        postgres_database, http_business, tmp_path):
    with runtime_service_journey(postgres_database, http_business, tmp_path,
                                  case_name="worker-killed") as h:
        compiled = h.register_forward()
        unit = h.approve_and_admit(compiled, "forward-approval")["unit_digest"]
        with orders(h) as (journal, client):
            before = OrderIntent("before", "worker-customer", "before", "10")
            denied = OrderIntent("fenced", "worker-customer", "fenced", "20")
            assert client.submit(before) == 200
            a = WorkerProcess(h, label="worker-a", pause_after="copy_content")
            b = None
            try:
                a.wait(a.ready.exists)
                assert client.submit(denied) == 503
                authority_before = h.status(unit)
                a.kill_after_apply()
                a_result = a.stop()
                # Observe actual DB lease expiry; do not backdate the row.
                import time
                deadline = time.monotonic() + 10
                while not execution(h)["lease_expired"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                expired = execution(h)
                assert expired["lease_expired"] and expired["owner"] == a.owner
                copied = observe(http_business, http_business["names"]["tgt"], journal)
                assert len(copied["orders"]) == 1  # real copy committed before SIGKILL
                b = WorkerProcess(h, label="worker-b")
                b.wait(lambda: any(e["event"] == "drive_returned" and e["owner"] == b.owner and e["state"] is None
                                   for e in b.events()))
                b_result = b.stop()
                assert h.status(unit) == authority_before
                assert h.route.get() == http_business["names"]["src"]
                final = execution(h)
                assert final["owner"] == a.owner and final["epoch"] == expired["epoch"]
                pending = [e for e in final["effects"] if e["action"] == "copy_content"]
                assert len(pending) == 1 and pending[0]["status"] == "in_flight"
                events = a.events()
                applied_copy_once(events)
                assert not any(e["event"] == "apply_started" and e["owner"] == b.owner for e in events)
                observed = observe(http_business, http_business["names"]["src"], journal)
                result = reconcile(journal.snapshot(), observed)
                assert result["counts"]["confirmed_requests"] == 1
                assert result["counts"]["not_committed_requests"] == 1
                h.record("worker-killed", authority=h.status(unit), execution=final,
                         worker_processes=[a_result, b_result], trace=events,
                         client_journal=journal.snapshot(), independent_observation=observed, measurement=result,
                         outcome="RECOVERY_REQUIRED; no quiescence provider, no automatic retry or write reopening",
                         migration_completed=False, worker_cli_deployed=False)
            finally:
                a.stop()
                if b:
                    b.stop()
