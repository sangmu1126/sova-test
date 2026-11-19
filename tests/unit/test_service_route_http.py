"""Proxy Pod readiness alone must not prove the actual upstream route."""

import pytest

from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls, KubernetesServiceRoute
from sova.domain.contracts import ContractError
from sova.service.route_stores import route_from


class Controls(KubernetesHandoffControls):
    def __init__(self):
        self.foreign = False

    def service(self, name):
        return {"spec": {"selector": {"side": "source"}}}

    def endpoints(self, service):
        return [("foreign" if self.foreign else "edge-source", True)]

    def pods(self, selector):
        return {"edge-source"}


def test_ready_proxy_pods_do_not_hide_a_stale_or_unhealthy_upstream():
    answers = iter(["source", "http-status:503", "source"])
    store = KubernetesServiceRoute(Controls(), "entry", {
        "source": {"side": "source"}, "target": {"side": "target"}},
        observe=lambda: next(answers), samples=3)
    seen = store.observe()
    assert seen["ready_endpoints"] == 1
    assert seen["route"] is None
    assert seen["http_sides"][1] == "http-status:503"


def test_all_actual_http_samples_must_agree_with_control_plane_side():
    for answers, wanted in [(["source"] * 3, "source"),
                            (["source", "target", "source"], None)]:
        values = iter(answers)
        store = KubernetesServiceRoute(Controls(), "entry", {
            "source": {"side": "source"}, "target": {"side": "target"}},
            observe=lambda: next(values), samples=3)
        assert store.get() == wanted


def test_foreign_endpoint_never_becomes_valid_because_http_looks_right():
    controls = Controls()
    controls.foreign = True
    store = KubernetesServiceRoute(controls, "entry", {
        "source": {"side": "source"}, "target": {"side": "target"}}, observe=lambda: "source")
    assert store.get() is None


@pytest.mark.parametrize("samples", [0, 9, True, 1.5])
def test_unbounded_or_boolean_sample_counts_are_refused(samples):
    with pytest.raises(ContractError, match="samples"):
        KubernetesServiceRoute(Controls(), "entry", {
            "source": {"side": "source"}, "target": {"side": "target"}},
            observe=lambda: "source", samples=samples)


def test_service_execution_configuration_can_bind_a_real_http_probe():
    spec = {"kind": "kubernetes_service", "registration_file": "unused", "service": "entry",
            "sides": {"source": {"side": "source"}, "target": {"side": "target"}},
            "probe": {"url": "https://entry.example/healthz", "header": "X-Sova-Side",
                      "values": {"source": "source", "target": "target"}}, "samples": 3}
    route = route_from(spec, controls_for=lambda spec: Controls())
    assert route.http_observe is not None
    del spec["probe"]
    with pytest.raises(ContractError, match="require a route probe"):
        route_from(spec, controls_for=lambda spec: Controls())


class IdentifiedControls(Controls):
    def __init__(self):
        super().__init__()
        from types import SimpleNamespace
        self.registration = SimpleNamespace(namespace="owned-trial")
        self.uid, self.revision, self.patches = "registered-service", "12", []

    def service(self, name):
        return {"metadata": {"uid": self.uid, "resourceVersion": self.revision},
                "spec": {"selector": {"side": "source"}}}

    def _request(self, method, path, query, *, timeout, body=None, **kwargs):
        self.patches.append(body)
        return {}


def identified_route(controls, **kwargs):
    return KubernetesServiceRoute(controls, "entry", {
        "source": {"side": "source"}, "target": {"side": "target"}},
        expected_service_uid="registered-service", converge_seconds=0, **kwargs)


def test_recreated_service_cannot_be_changed_or_accepted_by_its_old_name():
    controls = IdentifiedControls()
    controls.uid = "replacement-service"
    route = identified_route(controls)
    for action in (lambda: route.set("target"), lambda: route.pause("target"), route.get):
        with pytest.raises(ContractError, match="Service was replaced"):
            action()
    assert controls.patches == []


def test_patch_tests_both_frozen_uid_and_current_resource_version():
    import json
    controls = IdentifiedControls()
    identified_route(controls).pause("target")
    patch = json.loads(controls.patches[-1])
    assert {"op": "test", "path": "/metadata/uid", "value": "registered-service"} in patch
    assert {"op": "test", "path": "/metadata/resourceVersion", "value": "12"} in patch


def test_http_samples_do_not_hide_service_change_during_observation():
    controls = IdentifiedControls()

    def http():
        controls.revision = "13"
        return "source"

    result = identified_route(controls, observe=http).observe()
    assert result["route"] is None and result["service_changed_during_probe"]
