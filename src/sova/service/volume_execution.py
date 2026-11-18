"""Execution of Kubernetes volume handoffs for the worker.

The volume adapter and its independent probes, measured like any data store
(``data_measure``), except that a business invariant is the registered
query of the unit's own database (``KubernetesVolumeProbes.invariant``).

An execution-file entry for a volume unit (``engine: kubernetes-volume``)::

    {"tenant_id": "...", "application_id": "...", "adapter_id": "...",
     "engine": "kubernetes-volume", "mode": "offline_copy",
     "volume": {
        "source": {"registration_file": "...", "token_file": "...",   # token: optional,
                   "claims": ["board-data"],                         #  default this Pod's
                   "app": ["deployment", "board"], "others": [["cronjob", "backup"]]},
        "target": {...the same keys...},
        "routes": ["<source route value>", "<target route value>"],
        "agent_image": "<image>@sha256:<digest>", "agent_key_file": "...",
        "run_as": [10001, 10001], "fs_group": 10001,                    # fs_group optional
        "storage_class": "...", "app_replicas": 1,                      # optional
        "write_probe": {"port": 8080, "path": "/probe"},                # optional
        "databases": ["board.db"],                                      # SQLite paths
        "invariants": {"BOARD-01": {"database": "board.db", "files": "SELECT ...",
                                     "under": "uploads"}},
        "zone_of": {"source": "<zone>", "target": "<zone>"},
        "other_tenant": {"registration_file": "...", "token_file": "...", "claim": "..."},
        "scratch_claim": "sova-restore",
        "agent_transport": "port-forward",                              # optional
        "kubelet_tls_verified": {"source": true, "target": true}},      # inventory
     "route": {"kind": "kubernetes_service", ...}}

Each side's registration pins its API server; the token is that cluster's
(another cluster's token is a mounted file of its own). The agent key is read
from its file, never from the execution file. ``agent_transport``
``port-forward`` reaches the agents through the API server's port-forward
(the token then needs ``pods/portforward`` ``create``) instead of its Pod
proxy, so no data crosses the cluster network in the clear;
``kubelet_tls_verified`` is the operator's record that each cluster's API
server verifies its kubelets' certificates, which a client cannot observe.

The adapter and the probes each keep their reader agents from one step to the
next while the worker runs the execution's steps in a row - on a managed
cluster every new agent costs seconds inside the write stop - and the
execution's ``close`` reaps them. They never share agents with each other:
the probes measure apart from the adapter that moved the files.
"""

from pathlib import Path

from sova.adapters.kubernetes.registration import decode_registration
from sova.adapters.kubernetes.volume_agents import VolumeAgents
from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls
from sova.adapters.kubernetes_volume_handoff import KubernetesVolumeHandoffAdapter, VolumeSide
from sova.adapters.kubernetes_volume_probes import KubernetesVolumeProbes
from sova.adapters.registration.graph_codec import json_document
from sova.domain.contracts import ContractError
from sova.service.postgres_execution import data_measure
from sova.service.worker import Execution

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_VOLUME_KEYS = {"source", "target", "routes", "agent_image", "agent_key_file", "run_as",
                "fs_group", "storage_class", "app_replicas", "write_probe", "databases",
                "invariants", "zone_of", "other_tenant", "scratch_claim", "agent_transport",
                "kubelet_tls_verified"}
_VOLUME_REQUIRED = {"source", "target", "routes", "agent_image", "agent_key_file", "run_as",
                    "scratch_claim"}
_SIDE_KEYS = {"registration_file", "token_file", "ca_file", "claims", "app", "others"}


def volume_measure(probes, *, scratch):
    base = data_measure(probes, other_db=None, scratch_db=scratch)

    def measure(obligation, times):
        if obligation["kind"] == "business_invariant":
            return probes.invariant(obligation["id"].split(".", 1)[1])
        return base(obligation, times)
    return measure


def volume_factory(adapter_id, resolve):
    """A catalog factory: ``resolve(tenant, application)`` gives the sides
    (``VolumeSide``), the route store and its two values, and the agent and
    probe settings."""
    def factory(tenant_id, application_id, revision):
        d = resolve(tenant_id, application_id)
        adapter = KubernetesVolumeHandoffAdapter(
            adapter_id, source=d["source"], target=d["target"], route=d["route"],
            routes=d["routes"], agent_key=d["agent_key"], image=d["agent_image"],
            run_as=d["run_as"], fs_group=d.get("fs_group"), storage_class=d.get("storage_class"),
            app_replicas=d.get("app_replicas", 1),
            agent_transport=d.get("agent_transport", "proxy"), keep_agents=True)
        # The probes keep readers of their own, apart from the adapter's.
        agents = VolumeAgents(label="probe" + adapter.unit[:10], key=d["agent_key"],
                              image=d["agent_image"], run_as=d["run_as"],
                              fs_group=d.get("fs_group"),
                              transport=d.get("agent_transport", "proxy"), keep=True)
        probes = KubernetesVolumeProbes(
            source=d["source"], target=d["target"], agents=agents,
            write_probe=d.get("write_probe"), databases=d.get("databases", ()),
            invariants=d.get("invariants"), zone_of=d.get("zone_of"), other=d.get("other"),
            kubelet_tls_verified=d.get("kubelet_tls_verified"))
        def close():
            try:
                adapter.release_agents()
            finally:
                agents.release()
        return Execution(adapter, volume_measure(probes, scratch=d["scratch_claim"]),
                         close=close)

    return factory


def _read(path, limit=65536):
    data = Path(path).read_bytes()
    if not data.strip() or len(data) > limit:
        raise ContractError(f"Unreadable configuration file {path}")
    return data


def controls_from(spec, *, service_account=SERVICE_ACCOUNT):
    """Volume controls for one side: its registration, that cluster's CA and token."""
    registration = decode_registration(json_document(_read(spec["registration_file"])
                                                     .decode("utf-8")))
    ca = _read(spec.get("ca_file") or service_account / "ca.crt")
    token_file = Path(spec.get("token_file") or service_account / "token")
    return KubernetesVolumeControls(
        registration, ca_pem=ca,  # the token is rotated: read at every use
        token_provider=lambda: token_file.read_text().strip())


def _ref(value, what):
    if type(value) is not list or len(value) != 2 or not all(type(v) is str for v in value):
        raise ContractError(f"{what}: [kind, name] required")
    return tuple(value)


def volume_resolver(app, *, route_for, controls_for=controls_from):
    """``resolve(tenant, application)`` for one configured volume unit."""
    volume = app.get("volume")
    if type(volume) is not dict or set(volume) - _VOLUME_KEYS or _VOLUME_REQUIRED - set(volume):
        raise ContractError("Volume execution entry has missing or unknown keys")
    sides = {}
    for side in ("source", "target"):
        spec = volume[side]
        if type(spec) is not dict or set(spec) - _SIDE_KEYS or not {
                "registration_file", "claims", "app"} <= set(spec):
            raise ContractError(f"{side}: registration_file, claims and app required")
        claims = spec["claims"]
        if type(claims) is not list or not claims or not all(type(c) is str for c in claims):
            raise ContractError(f"{side}: claims required")
        sides[side] = VolumeSide(controls_for(spec), tuple(claims), _ref(spec["app"], "app"),
                                 tuple(_ref(o, "writer") for o in spec.get("others") or ()))
    routes = volume["routes"]
    if type(routes) is not list or len(routes) != 2:
        raise ContractError("routes: [source value, target value] required")
    run_as = volume["run_as"]
    if type(run_as) is not list or len(run_as) != 2:
        raise ContractError("run_as: [user, group] required")
    probe = volume.get("write_probe")
    if probe is not None and (type(probe) is not dict or set(probe) != {"port", "path"}):
        raise ContractError("write_probe: port and path required")
    transport = volume.get("agent_transport", "proxy")
    if transport not in ("proxy", "port-forward"):
        raise ContractError("agent_transport: proxy or port-forward")
    verified = volume.get("kubelet_tls_verified") or {}
    if type(verified) is not dict or set(verified) - {"source", "target"} or not all(
            type(v) is bool for v in verified.values()):
        raise ContractError("kubelet_tls_verified: true or false per side")
    zones = volume.get("zone_of") or {}
    if type(zones) is not dict or set(zones) - {"source", "target"}:
        raise ContractError("zone_of: source and target labels")
    other = volume.get("other_tenant")
    if other is not None:
        if type(other) is not dict or "claim" not in other:
            raise ContractError("other_tenant: registration_file and claim required")
        other = (controls_for(other), other["claim"])
    key = _read(volume["agent_key_file"]).strip()
    try:
        agent_key = bytes.fromhex(key.decode("ascii"))
    except ValueError as error:
        raise ContractError("The agent key file holds hex") from error
    resolved = {
        **sides, "route": route_for(app["route"]), "routes": tuple(routes),
        "agent_key": agent_key, "agent_image": volume["agent_image"], "run_as": tuple(run_as),
        "fs_group": volume.get("fs_group"), "storage_class": volume.get("storage_class"),
        "app_replicas": volume.get("app_replicas", 1),
        "write_probe": (probe["port"], probe["path"]) if probe else None,
        "databases": tuple(volume.get("databases") or ()),
        "invariants": volume.get("invariants") or {},
        "zone_of": (lambda side: zones.get(side)) if zones else None, "other": other,
        "scratch_claim": volume["scratch_claim"],
        "agent_transport": transport,
        "kubelet_tls_verified": (lambda side: verified.get(side) is True) if verified else None}

    def resolve(tenant_id, application_id):
        if (tenant_id, application_id) != (app["tenant_id"], app["application_id"]):
            raise ContractError("No execution configured for this application")
        return resolved

    return resolve, lambda: None
