"""Real browser/IdP -> native API -> PG ledger -> polling worker -> HTTP app.

Registry/evidence trust, change feeds and the file route are explicit local
fixtures. This is not AWS/VM, a deployment qualification or a continuity SLO.
"""

from datetime import timedelta
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pytest
from contract_world import build_world
from qualification_orders import observe
from qualification_runtime import RuntimeJourney, private_file
from qualification_worker import WorkerProcess
from qualification_workspace_oidc import identity_provider
from test_order_http_handoff import http_business as http_business  # noqa: F401
from test_service_control_plane import feeds_complete, request_of
from test_worker_process_journey import applied_copy_once, orders

from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.registration import registry_codec as codecs
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.plan_compiler import FAILBACK_STRATEGY
from sova.adapters.postgres_governance import PostgresSecurityHolds
from sova.domain.contracts import ResourceScope
from sova.security.domain.governance import SecurityHold
from sova.qualification.requests import OrderIntent, reconcile

pytestmark = pytest.mark.postgres


def checkpoint(path, process, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        if process.poll() is not None:
            raise AssertionError("Browser stopped before its checkpoint; inspect redacted artifact")
        time.sleep(0.05)
    raise AssertionError("Browser checkpoint timed out")


def bootstrap_evidence(h, compiled):
    """Same explicit fixture trust/feed bootstrap as the earlier service suite.

    Does not approve or admit; those actions must come from browser users.
    """
    lifecycle = PostgresLifecycleRepository(h.connect, tenant_id=h.tenant)
    plan = lifecycle.get_plan(h.app, compiled["plan_id"])
    at = h.clock() - timedelta(seconds=1)
    h.scopes.update(o.scope for o in plan.obligations)
    h.trust_version += 1
    h.publish_trust(VerifierTrust(h.signer.issuer_id, h.signer.key_id,
        h.signer.public_key_hex, tuple(sorted(h.scopes,
            key=lambda s: (s.environment_id, s.resource_id))), (h.settings.evidence_verifier,),
        at - timedelta(minutes=1), at + timedelta(hours=1)), version=h.trust_version)
    feeds_complete({"connect": h.connect, "tenant": h.tenant}, compiled)
    return plan


@pytest.mark.parametrize("outcome", ("complete", "unknown", "failback"))
def test_browser_registers_compiles_independently_approves_and_observes_real_effects(
        postgres_database, http_business, tmp_path, outcome):
    assert os.environ.get("SOVA_TEST_RECOVERY_BROWSER_TOOLS"), "Explicit browser driver required"
    h = RuntimeJourney(postgres_database, http_business, tmp_path)
    h.case_name = "workspace-user-journey-" + outcome
    artifacts = Path(os.environ["SOVA_TEST_QUALIFICATION_ARTIFACTS"])
    visible = artifacts / ("workspace-browser-" + outcome)
    visible.mkdir(mode=0o700)
    worker = successor = returned_worker = browser = None
    browser_result = None
    browser_collected = False
    report = {"fixture_inventory_and_trust": True, "fixture_route": True,
              "operational_qualification": False, "aws_vm_migration": False}
    try:
        with identity_provider(h) as idp:
            h.start_api()
            w = h.world
            config = {"origin": h.origin, "idpOrigin": idp["origin"], "users": idp["users"],
                "application": h.app, "tenant": h.tenant, "plan": request_of(w).document,
                "registrations": [{"kind": codecs.kind_of(v), "subject": codecs.subject_of(v),
                    "document": codecs.encode(v)} for v in (w["contract"], w["snapshot"],
                        w["source"], w["target"], w["manifest"], w["qualification"])],
                "attestations": [w["sign"](v).document for v in
                    (w["contract"], w["snapshot"], w["qualification"])],
                "tools": os.environ["SOVA_TEST_RECOVERY_BROWSER_TOOLS"],
                "executable": os.environ["SOVA_TEST_RECOVERY_BROWSER_EXECUTABLE"],
                "directory": str(visible), "outcome": outcome}
            if outcome == "failback":
                reverse = build_world(h.clock() - timedelta(seconds=1), contract=w["contract"],
                    strategy=FAILBACK_STRATEGY, reverse=True,
                    environment="synthetic-target", uid_prefix="target-uid-")
                h.bind_database_versions(reverse)
                reverse["snapshot"] = replace(reverse["snapshot"], snapshot_id=reverse["snapshot"].snapshot_id + "-return")
                reverse["qualification"] = replace(reverse["qualification"], record_id="test-record-return")
                config["reverse"] = {"plan": request_of(reverse).document,
                    "registrations": [{"kind": codecs.kind_of(v), "subject": codecs.subject_of(v),
                        "document": codecs.encode(v)} for v in
                            (reverse["snapshot"], reverse["manifest"], reverse["qualification"])],
                    "attestations": [w["sign"](v).document for v in
                        (reverse["snapshot"], reverse["qualification"])]}
            with orders(h) as (journal, client):
                assert client.submit(OrderIntent("before", "ui-customer", "before", "10")) == 200
                browser = subprocess.Popen([os.environ["SOVA_TEST_RECOVERY_BROWSER_NODE"],
                    str(Path(__file__).resolve().parents[1] / "qualification_workspace_browser.mjs")],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                browser.stdin.write(json.dumps(config))
                browser.stdin.close()
                browser.stdin = None
                compiled = checkpoint(visible / "compiled.json", browser)
                plan = bootstrap_evidence(h, compiled)
                assert plan.created_by == idp["users"]["planner"]["subject"]
                private_file(visible / "bootstrap.ready", "{}")
                admitted = checkpoint(visible / "admitted.json", browser)
                revision = PostgresCompiledPlans(h.connect, tenant_id=h.tenant).load(compiled["compiled_digest"])
                assert admitted["unit_digest"] == json.loads(revision.canonical)["unit"]["unit_digest"]
                # The browser already enqueued the operation. This independent
                # process consumes the existing ledger rather than a UI shortcut.
                worker = WorkerProcess(h, label="workspace-worker", pause_after="copy_content",
                    stop_before_stabilization=outcome == "failback")
                worker.wait(worker.ready.exists)
                assert client.submit(OrderIntent("during", "ui-customer", "during", "20")) == 503
                if outcome in ("complete", "failback"):
                    worker.release.touch(mode=0o600)
                    if outcome == "complete":
                        worker.wait(lambda: any(e["event"] == "drive_returned" and
                            e.get("state") == "HANDOFF_COMPLETE" for e in worker.events()))
                    else:
                        worker.wait(lambda: any(e["event"] == "fixture_stop_before_stabilization"
                            for e in worker.events()))
                        worker.process.wait(timeout=5)
                        report["forward_worker"] = worker.stop()
                    assert h.route.get() == http_business["names"]["tgt"]
                    assert client.submit(OrderIntent("during", "ui-customer", "during", "20")) == 200
                    assert client.submit(OrderIntent("after", "ui-customer", "after", "30")) == 200
                    expected_requests, expected_errors = 3, 1
                else:
                    worker.kill_after_apply()
                    report["killed_worker"] = worker.stop()
                    hold_scope = ResourceScope(h.tenant, h.app, h.world["source"].profile_id, "database")
                    PostgresSecurityHolds(h.connect, tenant_id=h.tenant).open_hold(SecurityHold(
                        "workspace-incident", hold_scope, "test-security-collector",
                        "Explicit local incident fixture after committed copy", h.clock()))
                    # Same ledger, no quiescence proof: lease expiry cannot
                    # authorize replay or silently clear a security incident.
                    time.sleep(2.5)
                    successor = WorkerProcess(h, label="workspace-successor")
                    successor.wait(lambda: any(e["event"] == "drive_returned" and
                        e["owner"] == successor.owner and e.get("state") is None
                        for e in successor.events()))
                    report["successor"] = successor.stop()
                    assert h.route.get() == http_business["names"]["src"]
                    assert client.submit(OrderIntent("during", "ui-customer", "during", "20")) == 503
                    expected_requests, expected_errors = 1, 2
                applied_copy_once(worker.events())
                private_file(visible / "execution.ready", "{}")
                if outcome == "failback":
                    returned = checkpoint(visible / "reverse-compiled.json", browser)
                    return_plan = bootstrap_evidence(h, returned)
                    assert return_plan.created_by == idp["users"]["planner"]["subject"]
                    assert return_plan.plan_id != plan.plan_id
                    private_file(visible / "reverse-bootstrap.ready", "{}")
                    reverse_admission = checkpoint(visible / "reverse-admitted.json", browser)
                    h.world = reverse
                    returned_worker = WorkerProcess(h, label="workspace-return-worker", reverse=True)
                    returned_worker.wait(lambda: any(e["event"] == "drive_returned" and
                        e["owner"] == returned_worker.owner and e.get("state") == "HANDOFF_COMPLETE"
                        for e in returned_worker.events()))
                    assert h.route.get() == http_business["names"]["src"]
                    assert client.submit(OrderIntent("returned", "ui-customer", "returned", "40")) == 200
                    assert client.lookup(OrderIntent("after", "ui-customer", "after", "30")) == 200
                    report.update(return_plan_id=return_plan.plan_id, return_admission=reverse_admission,
                        returned_worker=returned_worker.stop(), forward_completed=False)
                    expected_requests = 4
                    private_file(visible / "execution-final.ready", "{}")
                observed = observe(http_business,
                    http_business["names"]["src" if outcome == "failback" else "tgt"], journal)
                measurement = reconcile(journal.snapshot(), observed)
                # A known 503 rejection is not data loss. This assertion does
                # not resolve the killed worker's ambiguous command result.
                assert measurement["data_integrity"] == "SATISFIED"
                if outcome == "unknown":
                    assert measurement["counts"]["not_committed_requests"] == 1
                assert measurement["counts"]["confirmed_requests"] == expected_requests
                assert measurement["counts"]["http_error_attempts"] == expected_errors
                stdout, stderr = browser.communicate(timeout=45)
                browser_collected = True
                browser_result = {"exit_code": browser.returncode}
                if browser.returncode:
                    for user in config["users"].values():
                        stderr = stderr.replace(user["password"], "[redacted]")
                    private_file(visible / "browser-error.log", stderr[-8000:])
                    raise AssertionError("Browser user journey failed; inspect redacted artifact")
                report.update(browser=json.loads(stdout), client_journal=journal.snapshot(),
                    independent_observation=observed, measurement=measurement,
                    worker_trace=worker.events(), worker=worker.stop(), plan_id=plan.plan_id)
                assert report["browser"]["independent_subjects"] is True
                assert report["browser"]["forward_plan_id"] == plan.plan_id
                for name in ("independent-approval", "execution-status", "mobile"):
                    report[name + "_sha256"] = hashlib.sha256((visible / (name + ".png")).read_bytes()).hexdigest()
    finally:
        if browser and not browser_collected:
            if browser.poll() is None:
                browser.kill()
            stdout, stderr = browser.communicate(timeout=10)
            if browser_result is None or browser.returncode:
                for user in config["users"].values():
                    stderr = stderr.replace(user["password"], "[redacted]")
                private_file(visible / "browser-error.log", stderr[-8000:])
        if worker:
            report["worker_cleanup"] = worker.stop()
        if successor:
            report["successor_cleanup"] = successor.stop()
        if returned_worker:
            report["returned_worker_cleanup"] = returned_worker.stop()
        h.stop_api()
        report["api_starts"] = h.starts
        report["api_exits"] = h.exits
        private_file(artifacts / ("workspace-user-journey-" + outcome + ".json"), json.dumps(report, indent=2))
