"""Execution of a unit made of several components (a volume and a session
server, a database and an object store) for the worker.

Every component performs every action (``CompositeHandoffAdapter``). An
obligation about who can write, where the target is or what is authoritative
is asked of every component and holds only if it holds for each; an
obligation about content, business invariants, restores and the target's data
controls is asked of the component that holds the data; a measured budget is
the plan's own timing and measured once.
"""

from sova.adapters.composite_handoff import CompositeHandoffAdapter
from sova.adapters.kubernetes_session_handoff import KubernetesSessionHandoffAdapter
from sova.adapters.kubernetes_session_probes import KubernetesSessionProbes, session_measure
from sova.domain.contracts import ContractError
from sova.service.worker import Execution, close_execution

DATA_KINDS = frozenset({"final_content_comparison", "business_invariant", "restore_drill",
                        "target_control_test", "measured_budget", "external_result_lookup"})


def composite_execution(adapter_id, executions, *, data):
    """``executions``: ``(name, Execution)`` pairs in action order; ``data`` names
    the one that answers for content."""
    names = [name for name, _ in executions]
    if data not in names:
        raise ContractError("The data component must be one of the components")
    adapter = CompositeHandoffAdapter(adapter_id, tuple((n, e.adapter) for n, e in executions))
    measures = dict((n, e.measure) for n, e in executions)

    def measure(obligation, times):
        if obligation["kind"] in DATA_KINDS:
            return measures[data](obligation, times)
        results = {name: m(obligation, times) for name, m in measures.items()}
        return all(passed for passed, _ in results.values()), {
            "probe": "composite",
            "components": {name: {"passed": p, **o} for name, (p, o) in results.items()}}

    def close():
        for _, execution in executions:  # each one, whatever another's ending does
            close_execution(execution)
    return Execution(adapter, measure, close=close)


def session_execution(adapter_id, d):
    """The session adapter and its probes from resolved settings."""
    adapter = KubernetesSessionHandoffAdapter(
        adapter_id, source=d["source"], target=d["target"], route=d["route"],
        routes=d["routes"], port=d["port"], image=d["image"], run_as=d["run_as"],
        drain_seconds=d.get("drain_seconds", 300.0), on_timeout=d.get("on_timeout", "hold"),
        app_replicas=d.get("app_replicas", 1))
    probes = KubernetesSessionProbes(source=d["source"], target=d["target"], route=d["route"],
                                     routes=d["routes"], zone_of=d.get("zone_of"))
    return Execution(adapter, session_measure(probes))


_SESSION_KEYS = {"source", "target", "routes", "port", "image", "run_as", "drain_seconds",
                 "on_timeout", "app_replicas", "zone_of"}
_SESSION_SIDE_KEYS = {"registration_file", "token_file", "ca_file", "app"}


def session_resolver(app, *, route_for, controls_for):
    """Resolved settings of one session-server entry (``engine: kubernetes-session``)::

        "session": {"source": {"registration_file": "...", "app": ["deployment", "game"]},
                    "target": {...}, "routes": ["<source>", "<target>", "<paused>"],
                    "port": 8080, "image": "<image>@sha256:<digest>", "run_as": [10001, 10001],
                    "drain_seconds": 300, "on_timeout": "hold" | "terminate",
                    "app_replicas": 1, "zone_of": {"target": "<zone>"}}
    """
    from sova.adapters.kubernetes_session_handoff import SessionSide

    session = app.get("session")
    if type(session) is not dict or set(session) - _SESSION_KEYS or not {
            "source", "target", "routes", "port", "image", "run_as"} <= set(session):
        raise ContractError("Session execution entry has missing or unknown keys")
    sides = {}
    for side in ("source", "target"):
        spec = session[side]
        if type(spec) is not dict or set(spec) - _SESSION_SIDE_KEYS or not {
                "registration_file", "app"} <= set(spec):
            raise ContractError(f"{side}: registration_file and app required")
        if type(spec["app"]) is not list or len(spec["app"]) != 2:
            raise ContractError(f"{side}: app as [kind, name] required")
        sides[side] = SessionSide(controls_for(spec), tuple(spec["app"]))
    routes, run_as = session["routes"], session["run_as"]
    if type(routes) is not list or len(routes) != 3:
        raise ContractError("routes: [source, target, paused] required")
    if type(run_as) is not list or len(run_as) != 2:
        raise ContractError("run_as: [user, group] required")
    zones = session.get("zone_of") or {}
    if type(zones) is not dict or set(zones) - {"source", "target"}:
        raise ContractError("zone_of: source and target labels")
    if (app.get("route") or {}).get("kind") not in ("kubernetes_service", "gateway_httproute"):
        raise ContractError("A session server drains behind a Service selector or a "
                            "Gateway API route")
    return {**sides, "route": route_for(app["route"]), "routes": tuple(routes),
            "port": session["port"], "image": session["image"], "run_as": tuple(run_as),
            "drain_seconds": session.get("drain_seconds", 300.0),
            "on_timeout": session.get("on_timeout", "hold"),
            "app_replicas": session.get("app_replicas", 1),
            "zone_of": (lambda side: zones.get(side)) if zones else None}


_COMPONENT_KEYS = {"name", "adapter_id", "engine", "volume", "session", "route"}


def composite_resolver(app, *, route_for, controls_for):
    """``engine: composite``: ``components`` (each a volume or session entry with
    a ``name`` and its own ``adapter_id``, in action order) and ``data`` (the
    component answering for content). Returns a factory of the unit's Execution."""
    from sova.service.volume_execution import volume_factory, volume_resolver

    components, data = app.get("components"), app.get("data")
    if type(components) is not list or len(components) < 2:
        raise ContractError("components: at least two required")
    built = []
    for entry in components:
        if type(entry) is not dict or set(entry) - _COMPONENT_KEYS or not {
                "name", "adapter_id", "engine", "route"} <= set(entry):
            raise ContractError("A component needs name, adapter_id, engine and route")
        scoped = {**entry, "tenant_id": app["tenant_id"],
                  "application_id": app["application_id"]}
        if entry["engine"] == "kubernetes-volume":
            resolve, _ = volume_resolver(scoped, route_for=route_for, controls_for=controls_for)
            make = volume_factory(entry["adapter_id"], resolve)
        elif entry["engine"] == "kubernetes-session":
            settings = session_resolver(scoped, route_for=route_for, controls_for=controls_for)
            make = (lambda adapter_id, d: lambda *args: session_execution(adapter_id, d))(
                entry["adapter_id"], settings)
        else:
            raise ContractError("A component is a kubernetes-volume or kubernetes-session")
        built.append((entry["name"], make))
    if data not in {name for name, _ in built}:
        raise ContractError("data must name one of the components")

    def factory(tenant_id, application_id, revision):
        return composite_execution(app["adapter_id"], [
            (name, make(tenant_id, application_id, revision)) for name, make in built],
            data=data)
    return factory
