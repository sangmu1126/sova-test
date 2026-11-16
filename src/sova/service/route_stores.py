"""Route stores from the worker's execution file.

A route store is where a unit's traffic is switched and read back; it lives
outside the worker (several workers, restarts). Two kinds:

``kubernetes_service``
    a Service selector, read back from the endpoints traffic reaches::

        {"kind": "kubernetes_service", "registration_file": "...", "service": "orders",
         "sides": {"<side>": {"label": "value"}, ...}, "converge_seconds": 60}

``gateway_httproute``
    one rule of a Gateway API HTTPRoute, read back from its configuration, its
    parent Gateways' acceptance and real requests through the entry point::

        {"kind": "gateway_httproute", "registration_file": "...", "route": "web",
         "rule": 0, "parents": [["infra", "public"]],
         "sides": {"<side>": ["web-source", 80], ...},
         "probe": {"url": "https://shop.example.com/healthz", "header": "X-Sova-Side",
                   "values": {"<side>": "<marker>", ...}, "ca_file": null},
         "samples": 6, "converge_seconds": 60}

Each is reached with this Pod's own service account through the pinned
transport of a Kubernetes registration; a route in another cluster names that
cluster's ``token_file`` and ``ca_file``.
"""

from pathlib import Path

from sova.adapters.kubernetes.gateway_route import GatewayHttpRoute, http_side_observer
from sova.adapters.kubernetes.handoff_controls import (
    KubernetesHandoffControls,
    KubernetesServiceRoute,
)
from sova.adapters.kubernetes.registration import decode_registration
from sova.adapters.registration.graph_codec import json_document
from sova.domain.contracts import ContractError

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_SERVICE_KEYS = {"kind", "registration_file", "token_file", "ca_file", "service", "sides",
                 "converge_seconds"}
_GATEWAY_KEYS = {"kind", "registration_file", "token_file", "ca_file", "route", "rule",
                 "parents", "sides", "probe", "samples", "converge_seconds"}
_PROBE_KEYS = {"url", "header", "values", "ca_file"}


def _read(path, limit=1 << 20):
    data = Path(path).read_bytes()
    if not data.strip() or len(data) > limit:
        raise ContractError(f"Unreadable configuration file {path}")
    return data.decode("utf-8").strip()


def handoff_controls(spec, *, service_account=SERVICE_ACCOUNT):
    """Controls for the registered namespace: its registration, the cluster's
    CA and a token that the kubelet rotates (read at every use)."""
    registration = decode_registration(json_document(_read(spec["registration_file"])))
    ca = Path(spec.get("ca_file") or service_account / "ca.crt").read_bytes()
    token_file = Path(spec.get("token_file") or service_account / "token")
    return KubernetesHandoffControls(
        registration, ca_pem=ca, token_provider=lambda: token_file.read_text().strip())


def route_from(spec, *, controls_for=handoff_controls):
    if type(spec) is not dict:
        raise ContractError("A route store entry required")
    kind = spec.get("kind")
    if kind == "kubernetes_service":
        if set(spec) - _SERVICE_KEYS:
            raise ContractError("Unknown route keys")
        return KubernetesServiceRoute(controls_for(spec), spec["service"], spec["sides"],
                                      converge_seconds=float(spec.get("converge_seconds", 60)))
    if kind == "gateway_httproute":
        if set(spec) - _GATEWAY_KEYS or not {"registration_file", "route", "parents", "sides",
                                             "probe"} <= set(spec):
            raise ContractError("Gateway route: known keys, route, parents, sides and probe")
        probe = spec["probe"]
        if type(probe) is not dict or set(probe) - _PROBE_KEYS or not {
                "url", "header", "values"} <= set(probe):
            raise ContractError("Gateway route probe: url, header and side values required")
        if type(probe["values"]) is not dict or set(probe["values"]) != set(spec["sides"]):
            raise ContractError("A marker value for every side required")
        parents = spec["parents"]
        if type(parents) is not list or not all(type(p) is list and len(p) == 2
                                                for p in parents):
            raise ContractError("parents: [[namespace, gateway], ...] required")
        sides = spec["sides"]
        if type(sides) is not dict or not all(type(v) is list and len(v) == 2
                                              for v in sides.values()):
            raise ContractError("sides: {side: [service, port]} required")
        observe = http_side_observer(probe["url"], header=probe["header"],
                                     sides=probe["values"], ca_file=probe.get("ca_file"))
        return GatewayHttpRoute(
            controls_for(spec), spec["route"], rule=spec.get("rule", 0),
            parents=tuple(tuple(p) for p in parents),
            sides={k: tuple(v) for k, v in sides.items()}, observe=observe,
            samples=spec.get("samples", 6),
            converge_seconds=float(spec.get("converge_seconds", 60)))
    raise ContractError("Route kind is kubernetes_service or gateway_httproute")
