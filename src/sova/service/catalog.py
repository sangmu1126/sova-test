"""The worker's adapter catalog, from its execution file.

What a worker may touch is deployment configuration, not request input: the
execution file (mounted from a Secret, ``SOVA_EXECUTION_FILE``) names, per
(tenant, application), the adapter, the application's databases, its writer
roles and the route store. Credentials stay in their own mounted files; the
execution file holds only their paths.

    {"version": 1,
     "applications": [{
        "tenant_id": "...", "application_id": "...",
        "adapter_id": "postgres-offline-copy",
        "mode": "offline_copy" | "logical_replication",
        "databases": {"<db>": {"dsn_file": "<admin conninfo, no dbname>"}, ...},
        "role_password_files": {"<role>": "<path>"},          # optional
        "source_db": "...", "target_db": "...", "scratch_db": "...", "other_db": "...",
        "app_role": "...", "writer_roles": ["...", ...],
        "tables": [["orders", "id"], ...],
        "schema_sql_file": "...", "probe_insert": "INSERT ...",
        "source_conninfo_file": "...",                        # logical_replication
        "route": {"kind": "kubernetes_service",
                  "registration_file": "...",                 # a Kubernetes registration
                  "service": "orders", "sides": {"<db>": {"label": "value"}, ...},
                  "converge_seconds": 60}}]}

The route store must be outside the worker (several workers, restarts): a
Kubernetes Service selector here, read back from the endpoints that traffic
actually reaches. Administrative connections are reused per database
(ADR-0004); a role probe always opens a fresh session.
"""

from pathlib import Path

import psycopg

from sova.adapters.kubernetes.handoff_controls import (
    KubernetesHandoffControls,
    KubernetesServiceRoute,
)
from sova.adapters.kubernetes.registration import decode_registration
from sova.adapters.postgres_connections import ReusedConnections
from sova.adapters.registration.graph_codec import json_document
from sova.domain.contracts import ContractError
from sova.service.postgres_execution import postgres_factory
from sova.service.worker import AdapterCatalog

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MODES = {"offline_copy": False, "logical_replication": True}
_APP_KEYS = {"tenant_id", "application_id", "adapter_id", "mode", "databases",
             "role_password_files", "source_db", "target_db", "scratch_db", "other_db",
             "app_role", "writer_roles", "tables", "schema_sql_file", "probe_insert",
             "source_conninfo_file", "route"}
_REQUIRED = _APP_KEYS - {"role_password_files", "source_conninfo_file"}


def _read(path, limit=1 << 20):
    data = Path(path).read_bytes()
    if not data.strip() or len(data) > limit:
        raise ContractError(f"Unreadable configuration file {path}")
    return data.decode("utf-8").strip()


def _strings(value, what):
    if type(value) is not list or not value or not all(type(v) is str and v for v in value):
        raise ContractError(f"{what}: non-empty strings required")
    return tuple(value)


def kubernetes_route(spec, *, service_account=SERVICE_ACCOUNT):
    """A Service-selector route store reached with this pod's own service
    account, through the pinned transport of a Kubernetes registration."""
    if set(spec) - {"kind", "registration_file", "service", "sides", "converge_seconds"}:
        raise ContractError("Unknown route keys")
    registration = decode_registration(json_document(_read(spec["registration_file"])))
    ca = (service_account / "ca.crt").read_bytes()
    token_file = service_account / "token"  # rotated by the kubelet: read at every use
    controls = KubernetesHandoffControls(
        registration, ca_pem=ca, token_provider=lambda: token_file.read_text().strip())
    return KubernetesServiceRoute(controls, spec["service"], spec["sides"],
                                  converge_seconds=float(spec.get("converge_seconds", 60)))


def application_resolver(app, *, route_for=kubernetes_route):
    """``resolve(tenant, application)`` for one configured application."""
    if type(app) is not dict or set(app) - _APP_KEYS or _REQUIRED - set(app):
        raise ContractError("Application execution entry has missing or unknown keys")
    databases = app["databases"]
    if type(databases) is not dict or not databases:
        raise ContractError("The application's databases are required")
    for key in ("source_db", "target_db", "scratch_db", "other_db"):
        if app[key] not in databases:
            raise ContractError(f"{key} names no configured database")
    servers = {db: _read(entry["dsn_file"]) for db, entry in databases.items()}
    passwords = {role: path for role, path in (app.get("role_password_files") or {}).items()}
    writers = _strings(app["writer_roles"], "writer_roles")
    if app["app_role"] not in writers:
        raise ContractError("The application role must be a registered writer")
    tables = tuple(tuple(_strings(t, "table and key")) for t in app["tables"])
    if not tables or not all(len(t) == 2 for t in tables):
        raise ContractError("tables: [table, key column] pairs required")
    if app["route"].get("kind") != "kubernetes_service":
        raise ContractError("Only a Kubernetes Service route store is supported")
    if app["mode"] not in MODES:
        raise ContractError("mode is offline_copy or logical_replication")
    route = route_for(app["route"])
    admin = ReusedConnections(lambda database: psycopg.connect(servers[database],
                                                               dbname=database))

    def connect_as(database, role):
        extra = {"password": _read(passwords[role])} if role in passwords else {}
        return psycopg.connect(servers[database], dbname=database, user=role, **extra)

    resolved = {
        "connect": admin, "connect_as": connect_as, "source_db": app["source_db"],
        "target_db": app["target_db"], "scratch_db": app["scratch_db"],
        "other_db": app["other_db"], "app_role": app["app_role"], "writer_roles": writers,
        "tables": tables, "schema_sql": _read(app["schema_sql_file"]),
        "probe_insert": app["probe_insert"], "route": route,
        "source_conninfo": (_read(app["source_conninfo_file"])
                            if app.get("source_conninfo_file") else None)}

    def resolve(tenant_id, application_id):
        if (tenant_id, application_id) != (app["tenant_id"], app["application_id"]):
            raise ContractError("No execution configured for this application")
        return resolved

    return resolve, admin


def catalog_from(document, *, route_for=kubernetes_route):
    """(AdapterCatalog, close) from a parsed execution file."""
    if type(document) is not dict or document.get("version") != 1 or set(document) - {
            "version", "applications"}:
        raise ContractError("Execution file version 1 required")
    apps = document.get("applications")
    if type(apps) is not list or not apps:
        raise ContractError("At least one application is required")
    by_adapter, kept = {}, []
    for app in apps:
        resolve, admin = application_resolver(app, route_for=route_for)
        kept.append(admin)
        key = (app["tenant_id"], app["application_id"])
        entry = by_adapter.setdefault(app["adapter_id"], {"mode": app["mode"], "apps": {}})
        if entry["mode"] != app["mode"]:
            raise ContractError("One adapter ID, one mode")
        if key in entry["apps"]:
            raise ContractError("An application is configured twice")
        entry["apps"][key] = resolve

    def factory_of(adapter_id, entry):
        def resolve(tenant_id, application_id):
            found = entry["apps"].get((tenant_id, application_id))
            if found is None:
                raise ContractError("No execution configured for this application")
            return found(tenant_id, application_id)
        return postgres_factory(adapter_id, resolve, cdc=MODES[entry["mode"]])

    catalog = AdapterCatalog({adapter_id: factory_of(adapter_id, entry)
                              for adapter_id, entry in by_adapter.items()})

    def close():
        for admin in kept:
            admin.close()

    return catalog, close
