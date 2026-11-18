"""The Service dataplane check decides from its own connection's log: the
exchange goes on after the selector moved, or it does not; a check that
cannot run is an error, not a verdict; its Pods and Service always go."""

import json

import pytest

from sova.adapters.kubernetes import flow_check
from sova.adapters.kubernetes.flow_check import ServiceFlowCheck
from sova.domain.contracts import ContractError


class Fake:
    def __init__(self, after, *, ready=True, endpoints_go=True):
        self.after, self.ready, self.endpoints_go = after, ready, endpoints_go
        self.moved, self.pods, self.deleted, self.services = False, {}, [], []

    def create_pod(self, body):
        self.pods[body["metadata"]["name"]] = body

    def create_check_service(self, name, *, selector, port, labels):
        self.services.append(name)
        return {"spec": {"clusterIP": "10.0.0.9"}}

    def endpoints(self, service):
        if self.moved:
            return [] if self.endpoints_go else [("server", True)]
        return [("server", True)] if self.ready else []

    def service(self, name):
        return {"metadata": {"resourceVersion": "7"}}

    def set_service_selector(self, name, version, selector):
        assert selector["sova.dev/flow-role"] == "nobody"
        self.moved = True

    def pod(self, name):
        return {"status": {"containerStatuses": [{"state": {"running": {}}}]}}

    def container_log(self, pod, container, tail=1):
        line = self.after if self.moved else {"exchanged": 3}
        return json.dumps(line) + "\n"

    def delete_pod(self, name):
        self.deleted.append(name)

    def delete_service(self, name):
        self.deleted.append(name)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(flow_check.time, "sleep", lambda seconds: None)


def check(fake):
    return ServiceFlowCheck(fake, image="img", run_as=(10001, 10001), label="unit",
                            start_seconds=0.01, settle_seconds=0.01)


def gone(fake):
    """Every Pod and Service the check made is removed."""
    return set(fake.pods) | set(fake.services) <= set(fake.deleted)


def test_an_exchange_that_goes_on_survives():
    fake = Fake({"exchanged": 3 + 16})
    survive, detail = check(fake).measure()
    assert survive and detail["exchanges_after_move"] == 16 and gone(fake)


@pytest.mark.parametrize("after, counted", [
    ({"exchanged": 3, "error": "TimeoutError"}, 0),  # cut at the move (GKE, measured)
    ({"error": "TimeoutError"}, 0),                  # a line without its count
    ({"exchanged": 4}, 1),
])
def test_an_exchange_that_stops_does_not(after, counted):
    fake = Fake(after)
    survive, detail = check(fake).measure()
    assert not survive and gone(fake)
    assert detail["client_error"] == after.get("error")
    assert detail["exchanges_after_move"] == counted


def test_a_check_that_cannot_run_is_an_error_and_still_cleans_up():
    fake = Fake({"exchanged": 20}, ready=False)
    with pytest.raises(ContractError, match="endpoint"):
        check(fake).measure()
    assert gone(fake)  # what was made is removed
    fake = Fake({"exchanged": 20}, endpoints_go=False)
    with pytest.raises(ContractError, match="kept its endpoints"):
        check(fake).measure()
    assert gone(fake)


def test_the_check_pods_hold_nothing_and_run_restricted():
    fake = Fake({"exchanged": 20})
    check(fake).measure()
    for pod in fake.pods.values():
        spec = pod["spec"]
        assert spec["automountServiceAccountToken"] is False and "volumes" not in spec
        assert spec["securityContext"]["runAsNonRoot"] is True
        container = spec["containers"][0]["securityContext"]
        assert container["readOnlyRootFilesystem"] and container["capabilities"] == {
            "drop": ["ALL"]}
