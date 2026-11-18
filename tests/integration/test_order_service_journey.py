"""MQ-03 local service composition: real HTTP, existing worker, real SQL.

The forward handoff is deliberately interrupted before stabilization, not
reported as complete. A separately approved failback copies target-side orders
back. This is not acceptance of a deployed API, cloud path or continuity SLO.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from qualification_orders import application, observe, serve
from qualification_service import service_journey
from test_order_http_handoff import http_business as http_business  # noqa: F401
from test_service_control_plane import request_of

from sova.adapters.registration import registry_codec as codecs
from sova.domain.handoff_authority import HandoffState
from sova.qualification.http_orders import OrderHttpClient
from sova.qualification.requests import OrderIntent, RequestJournal, reconcile
from sova.service.worker import Execution

pytestmark = pytest.mark.postgres


class FixtureStopBeforeStabilization(Exception):
    """An intentional local worker exception; not proof of process death."""


def stop_before_stabilization(*, on_fence=lambda: None, on_target=lambda: None):
    def wrap(factory):
        def build(tenant, app, revision):
            execution = factory(tenant, app, revision)
            def measure(obligation, times):
                if obligation["id"] == "fence.source_writes_denied":
                    on_fence()
                if obligation["kind"] == "measured_budget":
                    on_target()
                    raise FixtureStopBeforeStabilization()
                return execution.measure(obligation, times)
            return Execution(execution.adapter, measure)
        return build
    return wrap


def stop_forward(h, compiled):
    admitted = h.approve_and_admit(compiled, "forward-approval")
    w = h.worker(forward_factory=stop_before_stabilization())
    with pytest.raises(FixtureStopBeforeStabilization):
        w.drive(h.tenant, h.app, admitted["unit_digest"])
    assert h.status(admitted["unit_digest"])["state"] == "ROUTE_CONFIRMED"
    return admitted


def request_return(h, compiled):
    path = f"{h.app_base}/plans/{compiled['plan_id']}/failback-requests"
    response = h.call("POST", path, role="operator", body={"reason": "service_regression"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "FAILBACK_REQUIRED"
    return response.json()


def plan_return(h, compiled, reverse):
    path = f"{h.app_base}/plans/{compiled['plan_id']}/failback-plans"
    response = h.call("POST", path, role="planner", body=request_of(reverse).document)
    assert response.status_code == 201, response.text
    return response.json()


def test_service_orders_survive_target_writes_and_separately_approved_failback(
        postgres_database, http_business, tmp_path):
    b = http_business
    with service_journey(postgres_database, b, tmp_path) as h:
        compiled = h.register_forward()
        admitted = h.approve_and_admit(compiled, "forward-approval")
        profile = Path(__file__).resolve().parents[2] / "examples/qualification/orders-http-postgres-v1.json"
        journal = RequestJournal.create(tmp_path / "orders.sqlite", run_id="service-" + uuid4().hex,
                                        tenant_id=h.tenant, profile_digest=hashlib.sha256(profile.read_bytes()).hexdigest())
        token = uuid4().hex
        intents = [OrderIntent(key, "service-customer", key, str(amount)) for key, amount in (
            ("before", 10), ("fenced", 20), ("target", 30), ("returned", 40))]
        try:
            with serve(application(b, h.route, journal.scope, token)) as origin:
                client = OrderHttpClient(origin, bearer_token=token, journal=journal)
                try:
                    assert client.submit(intents[0]) == 200
                    def fenced():
                        assert client.submit(intents[1]) == 503
                    def target():
                        assert h.route.get() == b["names"]["tgt"]
                        assert client.submit(intents[2]) == 200
                    w = h.worker(forward_factory=stop_before_stabilization(
                        on_fence=fenced, on_target=target))
                    with pytest.raises(FixtureStopBeforeStabilization):
                        w.drive(h.tenant, h.app, admitted["unit_digest"])
                    forward_status = h.status(admitted["unit_digest"])
                    assert forward_status["state"] == "ROUTE_CONFIRMED"
                    required = request_return(h, compiled)
                    assert request_return(h, compiled) == required  # lost-response retry, no revision bump
                    reverse = h.register_reverse()
                    returned = plan_return(h, compiled, reverse)
                    assert returned["plan_id"] != compiled["plan_id"]
                    # The forward approval is not approval of the reverse plan.
                    replay = h.call("POST", h.app_base + "/admissions", role="operator",
                                    body={"approval_id": "forward-approval"})
                    assert replay.status_code == 409, replay.text
                    assert h.status(admitted["unit_digest"])["state"] == "FAILBACK_REQUIRED"
                    denied = h.call("POST", h.app_base + "/admissions", role="operator",
                                    body={"approval_id": "missing-return-approval"})
                    assert denied.status_code == 404, denied.text
                    reverse_unit = h.approve_and_admit(returned, "return-approval")
                    return_open = h.status(reverse_unit["unit_digest"])
                    assert return_open["epoch"] == forward_status["epoch"] + 1
                    # A fresh worker reads all durable state; the old worker object is unused.
                    record = h.worker().drive(h.tenant, h.app, reverse_unit["unit_digest"])
                    assert record.state == HandoffState.HANDOFF_COMPLETE, record.reason
                    assert record.predecessor == compiled["compiled_digest"]
                    assert record.epoch == return_open["epoch"] + 1  # activation intent raises it again
                    assert record.owner_environment == h.world["source"].profile_id
                    assert h.route.get() == b["names"]["src"]
                    assert client.submit(intents[1]) == 200
                    assert client.submit(intents[3]) == 200
                    assert client.lookup(intents[2]) == 200  # target-created order now on original source
                finally:
                    client.close()
            observation = observe(b, b["names"]["src"], journal)
            result = reconcile(journal.snapshot(), observation)
            assert result["data_integrity"] == "SATISFIED"
            assert result["counts"]["confirmed_requests"] == 4
            assert result["counts"]["submission_attempts"] == 5
            assert result["counts"]["http_error_attempts"] == 1
            assert result["service_continuity"] == "NOT_QUALIFIED"
            returned_status = h.status(reverse_unit["unit_digest"])
            assert returned_status["state"] == "HANDOFF_COMPLETE"
            # Polling/restarting an already finished worker must not replay effects.
            again = h.worker().drive(h.tenant, h.app, reverse_unit["unit_digest"])
            assert again.state == HandoffState.HANDOFF_COMPLETE
            assert h.status(reverse_unit["unit_digest"]) == returned_status
            reject_nested = h.call("POST", f"{h.app_base}/plans/{returned['plan_id']}/failback-requests",
                                   role="operator", body={"reason": "operator_requested"})
            assert reject_nested.status_code == 422
            assert h.status(admitted["unit_digest"])["state"] == "FAILBACK_REQUIRED"
            response = h.call("GET", h.app_base + "/evidence")
            assert response.status_code == 200
            evidence = response.json()["evidence"]
            assert evidence and all(e["result"] == "pass" and e["observation_kept"] for e in evidence)
            # Evidence listed over HTTP must have its actual observation available.
            for entry in evidence:
                response = h.call("GET", h.app_base + "/observations/" + entry["artifact_digest"])
                assert response.status_code == 200, response.text
                body = response.json()["observation"]
                assert hashlib.sha256(json.dumps(
                    body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() == entry["artifact_digest"]
            h.record("service-handoff-and-failback", forward_status=forward_status,
                     return_open=return_open, returned_status=returned_status, client_journal=journal.snapshot(),
                     independent_observation=observation, measurement=result,
                     evidence=evidence, injected_fault="fixture exception before stabilization; not process kill")
        finally:
            journal.close()


def test_failback_api_rejects_wrong_identity_direction_and_self_approval(
        postgres_database, http_business, tmp_path):
    with service_journey(postgres_database, http_business, tmp_path) as h:
        compiled = h.register_forward()
        admitted = stop_forward(h, compiled)
        path = f"{h.app_base}/plans/{compiled['plan_id']}/failback-requests"
        for headers, status in (({}, 401), (h.issuer.token("another-tenant", "operator"), 403),
                                (h.issuer.token(h.tenant, "auditor"), 403)):
            response = h.call("POST", path, headers=headers, body={"reason": "operator_requested"})
            assert response.status_code == status, response.text
        for body in ({"reason": "arbitrary text"}, {"reason": []}, {"reason": "operator_requested", "approved": True}):
            assert h.call("POST", path, role="operator", body=body).status_code == 422
        assert h.status(admitted["unit_digest"])["state"] == "ROUTE_CONFIRMED"
        request_return(h, compiled)
        reverse = h.register_reverse()
        plan_path = f"{h.app_base}/plans/{compiled['plan_id']}/failback-plans"
        wrong = request_of(reverse).document | {"source_profile": h.world["source"].profile_id}
        assert h.call("POST", plan_path, role="planner", body=wrong).status_code == 422
        wrong = request_of(reverse).document | {"service_id": "other-application"}
        assert h.call("POST", plan_path, role="planner", body=wrong).status_code == 422
        returned = plan_return(h, compiled, reverse)
        response = h.call("POST", f"{h.app_base}/plans/{returned['plan_id']}/approvals",
                          headers=h.issuer.token(h.tenant, "planner", "approver", subject="planner-person"),
                          body={"approval_id": "self-approval"})
        assert response.status_code == 403
        assert h.route.get() == http_business["names"]["tgt"]  # requesting is not execution
        h.record("failback-authorization-denials", original_status=h.status(admitted["unit_digest"]))


def test_failback_rechecks_registered_inputs_after_approval(postgres_database, http_business, tmp_path):
    with service_journey(postgres_database, http_business, tmp_path) as h:
        compiled = h.register_forward()
        stop_forward(h, compiled)
        request_return(h, compiled)
        reverse = h.register_reverse()
        returned = plan_return(h, compiled, reverse)
        h.approve(returned, "return-approval")
        changed = replace(reverse["target"], revision=reverse["target"].revision + 1)
        path = f"{h.base}/registrations/environment_profile/{changed.profile_id}"
        response = h.call("PUT", path, role="registrar",
                          body={"document": codecs.encode(changed), "expected_revision": 1})
        assert response.status_code == 200, response.text
        response = h.call("POST", h.app_base + "/admissions", role="operator",
                          body={"approval_id": "return-approval"})
        assert response.status_code == 422, response.text
        assert h.route.get() == http_business["names"]["tgt"]
        h.record("stale-return-input-denied", rejection=response.json())


def test_terminal_and_not_activated_handoffs_cannot_be_reopened_for_failback(
        postgres_database, http_business, tmp_path):
    with service_journey(postgres_database, http_business, tmp_path) as h:
        compiled = h.register_forward()
        admitted = h.approve_and_admit(compiled, "forward-approval")
        request_path = f"{h.app_base}/plans/{compiled['plan_id']}/failback-requests"
        plan_path = f"{h.app_base}/plans/{compiled['plan_id']}/failback-plans"
        assert h.call("POST", request_path, role="operator", body={"reason": "operator_requested"}).status_code == 422
        assert h.call("POST", plan_path, role="planner", body=request_of(h.world).document).status_code == 422
        record = h.worker().drive(h.tenant, h.app, admitted["unit_digest"])
        assert record.state == HandoffState.HANDOFF_COMPLETE
        response = h.call("POST", request_path, role="operator", body={"reason": "operator_requested"})
        assert response.status_code == 422, response.text
        assert h.status(admitted["unit_digest"])["state"] == "HANDOFF_COMPLETE"
        h.record("terminal-handoff-remains-terminal", rejection=response.json())
