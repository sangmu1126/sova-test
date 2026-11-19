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
