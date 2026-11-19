"""The Gateway API route store's decisions, without a cluster: which rule shapes
name one side, what acceptance means, and the HTTPS side observer."""

import http.server
import threading
from types import SimpleNamespace

import pytest

from sova.adapters.kubernetes.gateway_route import GatewayHttpRoute, http_side_observer
from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls
from sova.domain.contracts import ContractError

SIDES = {"web_src": ("web-source", 80), "web_tgt": ("web-target", 80)}


class Controls(KubernetesHandoffControls):
    def __init__(self, route):
        self.registration = SimpleNamespace(namespace="shop")
        self.route, self.patches = route, []

    def _get(self, path, query=None):
        return self.route

    def _request(self, method, path, query, *, timeout, body=None, **options):
        self.patches.append(body)
        return {}


def route_object(backends, *, generation=3, observed=3, accepted="True", filters=None):
    rule = {"backendRefs": backends, **({"filters": filters} if filters else {})}
    return {"metadata": {"name": "web", "namespace": "shop", "generation": generation,
                         "resourceVersion": "41"},
            "spec": {"rules": [rule]},
            "status": {"parents": [{"parentRef": {"name": "public", "namespace": "infra"},
                                    "conditions": [
                                        {"type": "Accepted", "status": accepted,
                                         "observedGeneration": observed},
                                        {"type": "ResolvedRefs", "status": "True",
                                         "observedGeneration": observed}]}]}}


def store(route, answers=("web_tgt",)):
    seen = iter(answers * 10)
    return GatewayHttpRoute(Controls(route), "web", parents=(("infra", "public"),),
                            sides=SIDES, observe=lambda: next(seen), samples=3,
                            converge_seconds=0)


@pytest.mark.parametrize("backends, filters, side", [
    ([{"name": "web-target", "port": 80}], None, "web_tgt"),
    ([{"name": "web-target", "port": 80, "weight": 1},
      {"name": "web-source", "port": 80, "weight": 0}], None, "web_tgt"),  # drained one
    ([{"name": "web-target", "port": 80, "weight": 50},
      {"name": "web-source", "port": 80, "weight": 50}], None, None),  # a split
    ([{"name": "web-target", "port": 8080}], None, None),  # another port
    ([{"name": "web-target", "port": 80, "namespace": "elsewhere"}], None, None),
    ([{"name": "web-target", "port": 80, "kind": "ServiceImport", "group": "multicluster"}],
     None, None),
    ([{"name": "web-target", "port": 80}], [{"type": "RequestMirror"}], None),  # mirrored
])
def test_only_a_plain_backend_of_a_registered_side_names_a_side(backends, filters, side):
    assert store(route_object(backends, filters=filters)).observe()["configured"] == side


def test_acceptance_must_be_for_the_routes_current_generation():
    stale = store(route_object([{"name": "web-target", "port": 80}], observed=2))
    assert stale.observe()["accepted"] == {"infra/public": False} and stale.get() is None
    refused = store(route_object([{"name": "web-target", "port": 80}], accepted="False"))
    assert refused.get() is None


def test_every_sample_must_be_answered_by_the_configured_side():
    route = route_object([{"name": "web-target", "port": 80}])
    assert store(route).get() == "web_tgt"
    assert store(route, answers=("web_tgt", "web_src")).get() is None  # still draining


def test_a_switch_tests_the_version_it_read_and_refuses_what_it_does_not_own():
    route = route_object([{"name": "web-source", "port": 80}])
    s = store(route)
    s.set("web_tgt")
    patch = s.controls.patches[-1].decode()
    assert '"op": "test", "path": "/metadata/resourceVersion", "value": "41"' in patch
    split = store(route_object([{"name": "web-target", "port": 80, "weight": 50},
                                {"name": "web-source", "port": 80, "weight": 50}]))
    with pytest.raises(ContractError, match="not overwritten"):
        split.set("web_src")
    assert split.controls.patches == []


class Marked(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(getattr(self.server, "response_status", 200))
        self.send_header("X-Sova-Side", self.server.side)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_the_observer_reads_the_side_marker_and_never_guesses():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Marked)
    server.side = "target"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/healthz"
        observe = http_side_observer(url, header="X-Sova-Side",
                                     sides={"web_src": "source", "web_tgt": "target"})
        assert observe() == "web_tgt"
        server.response_status = 503
        assert observe() == "http-status:503"  # marker alone is not a healthy response
        server.response_status = 200
        server.side = "staging"
        assert observe() == "unmarked:staging"
    finally:
        server.shutdown()
    with pytest.raises(ContractError):  # plain HTTP only on loopback
        http_side_observer("http://shop.example.com/", header="X-Sova-Side",
                           sides={"web_src": "source"})


def gateway_spec(**changes):
    spec = {"kind": "gateway_httproute", "registration_file": "unused", "route": "web",
            "rule": 0, "parents": [["infra", "public"]],
            "sides": {"web_src": ["web-source", 80], "web_tgt": ["web-target", 80]},
            "probe": {"url": "https://shop.example.com/healthz", "header": "X-Sova-Side",
                      "values": {"web_src": "source", "web_tgt": "target"}}}
    spec.update(changes)
    return spec


def test_the_execution_file_names_a_gateway_route_store():
    from sova.service.route_stores import route_from

    route = route_from(gateway_spec(), controls_for=lambda spec: Controls({}))
    assert type(route).__name__ == "GatewayHttpRoute"
    assert route.parents == (("infra", "public"),) and route.sides == SIDES


@pytest.mark.parametrize("changes", [
    {"probe": {"url": "http://shop.example.com/", "header": "X-Sova-Side",
               "values": {"web_src": "source", "web_tgt": "target"}}},  # not HTTPS
    {"probe": {"url": "https://shop.example.com/", "header": "X-Sova-Side",
               "values": {"web_src": "source"}}},  # a side without a marker
    {"parents": ["infra/public"]},
    {"kind": "ingress"},
    {"unexpected": True},
])
def test_a_gateway_route_entry_that_does_not_add_up_is_refused(changes):
    from sova.service.route_stores import route_from

    with pytest.raises(ContractError):
        route_from(gateway_spec(**changes), controls_for=lambda spec: Controls({}))


def test_a_pause_leaves_the_rule_without_a_backend_and_reads_back_only_when_served_by_none():
    route = route_object([{"name": "web-source", "port": 80}])
    s = store(route, answers=("error:503",))
    s.pause()
    patch = s.controls.patches[-1].decode()
    assert '"path": "/spec/rules/0/backendRefs", "value": []' in patch
    paused = store(route_object([]), answers=("error:503",))
    assert paused.paused()[0] and paused.left("web_src")
    still = store(route_object([]), answers=("web_src", "error:503"))  # an old proxy
    assert not still.paused()[0]
    split = store(route_object([{"name": "web-target", "port": 80, "weight": 50},
                                {"name": "web-source", "port": 80, "weight": 50}]))
    with pytest.raises(ContractError, match="not overwritten"):
        split.pause()
    resumed = store(route_object([]))
    resumed.set("web_tgt")  # from a paused rule straight to the target
    assert '"value": [{"name": "web-target", "port": 80}]' in \
        resumed.controls.patches[-1].decode()
