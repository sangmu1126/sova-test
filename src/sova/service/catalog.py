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
        "engine": "postgresql" | "mysql",                      # default postgresql
                  # (a volume unit, "kubernetes-volume", has its own keys:
                  #  see sova.service.volume_execution)
        "mode": "offline_copy" | "logical_replication",        # MySQL: offline_copy |
                                                               #   replication
        "databases": {"<db>": {"dsn_file": "<admin connection, no database>"}, ...},
        "zone_of": {"<db>": "<zone label>"},                   # MySQL: where each runs
        "tls_endpoints": {"<db>": {"host": "...", "port": 5432,  # PostgreSQL, optional:
                                   "server_name": "...",         #   what transport
                                   "ca_file": "..."}},           #   refusals ask
        "role_password_files": {"<role>": "<path>"},          # optional
        "source_db": "...", "target_db": "...", "scratch_db": "...", "other_db": "...",
        "app_role": "...", "writer_roles": ["...", ...],
        "tables": [["orders", "id"], ...],
        "schema_sql_file": "...", "probe_insert": "INSERT ...",
        "source_conninfo_file": "...",                        # logical_replication
        "route": {"kind": "kubernetes_service",                # or gateway_httproute
                  "registration_file": "...",                 # a Kubernetes registration
                  "service": "orders", "sides": {"<db>": {"label": "value"}, ...},
                  "converge_seconds": 60}}]}           # (see sova.service.route_stores)

The route store must be outside the worker (several workers, restarts): a
Kubernetes Service selector here, read back from the endpoints that traffic
actually reaches. PostgreSQL administrative connections are reused
per database (ADR-0004); a role probe always opens a fresh session.

For PostgreSQL a ``dsn_file`` holds a libpq conninfo; for MySQL a JSON object
of connection settings (``host``, ``port``, ``user``, ``password``, and for
TLS ``ssl_ca`` with ``ssl_verify_identity``). Writer identities are roles in
PostgreSQL and ``user@host`` accounts in MySQL.
"""

import json
from pathlib import Path

from sova.domain.contracts import ContractError
from sova.service.worker import AdapterCatalog

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MODES = {"offline_copy": False, "logical_replication": True}
ENGINES = {"postgresql": set(MODES), "mysql": {"offline_copy", "replication"}}
_APP_KEYS = {"tenant_id", "application_id", "adapter_id", "engine", "mode", "databases",
             "role_password_files", "source_db", "target_db", "scratch_db", "other_db",
             "app_role", "writer_roles", "tables", "schema_sql_file", "probe_insert",
             "source_conninfo_file", "route", "zone_of", "replication", "tls_endpoints"}
_REQUIRED = _APP_KEYS - {"role_password_files", "source_conninfo_file", "engine", "zone_of",
                         "replication", "tls_endpoints"}
_REPLICATION_KEYS = {"source_endpoint", "account", "password_file", "tls", "ca_path",
                     "snapshot"}
_UNIT_ENGINES = {  # engines whose entries carry their own keys (see each module)
    "kubernetes-volume": {"volume", "route"},
    "kubernetes-session": {"session", "route"},
    "composite": {"components", "data"},
    "redis": {"redis", "route"},
    "kafka": {"kafka", "route"},
    "rabbitmq": {"rabbitmq", "route"},
    "mongodb": {"mongodb", "route"}}
_UNIT_KEYS = {"tenant_id", "application_id", "adapter_id", "engine", "mode"}
_UNIT_MODES = {"mongodb": {"offline_copy", "change_stream"}}  # others: offline_copy
_MYSQL_SETTINGS = {"host", "port", "user", "password", "ssl_ca", "ssl_verify_identity",
                   "connect_timeout"}


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
    """The route store an entry names (a Service selector or a Gateway API
    HTTPRoute), reached with this pod's own service account through the
    pinned transport of a Kubernetes registration (``route_stores``)."""
    from sova.service.route_stores import handoff_controls, route_from

    return route_from(spec, controls_for=lambda entry: handoff_controls(
        entry, service_account=service_account))


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
    engine = app.get("engine", "postgresql")
    if engine not in ENGINES:
        raise ContractError("engine is postgresql or mysql")
    passwords = {role: path for role, path in (app.get("role_password_files") or {}).items()}
    writers = _strings(app["writer_roles"], "writer_roles")
    if app["app_role"] not in writers:
        raise ContractError("The application role must be a registered writer")
    tables = tuple(tuple(_strings(t, "table and key")) for t in app["tables"])
    if not tables or not all(len(t) == 2 for t in tables):
        raise ContractError("tables: [table, key column] pairs required")
    if app["route"].get("kind") not in ("kubernetes_service", "gateway_httproute"):
        raise ContractError("The route store is a Kubernetes Service or a Gateway API route")
    if app["mode"] not in ENGINES[engine]:
        raise ContractError(f"mode {app['mode']} is not supported for {engine}")
    zones = app.get("zone_of") or {}
    if type(zones) is not dict or not all(type(v) is str for v in zones.values()):
        raise ContractError("zone_of maps databases to zone labels")
    tls = _tls_endpoints(app.get("tls_endpoints"), databases, engine,
                         sides={"source": app["source_db"], "target": app["target_db"]})
    route = route_for(app["route"])
    if engine == "mysql":
        admin, close, connect_as = _mysql_connections(databases, passwords)
    else:
        admin, close, connect_as = _postgres_connections(databases, passwords)

    resolved = {
        "connect": admin, "connect_as": connect_as, "source_db": app["source_db"],
        "target_db": app["target_db"], "scratch_db": app["scratch_db"],
        "other_db": app["other_db"], "app_role": app["app_role"], "writer_roles": writers,
        "tables": tables, "schema_sql": _read(app["schema_sql_file"]),
        "probe_insert": app["probe_insert"], "route": route,
        "zone_of": (lambda database: zones.get(database)) if zones else None,
        "source_conninfo": (_read(app["source_conninfo_file"])
                            if app.get("source_conninfo_file") else None),
        "replication": _replication(app.get("replication")), "tls": tls}

    def resolve(tenant_id, application_id):
        if (tenant_id, application_id) != (app["tenant_id"], app["application_id"]):
            raise ContractError("No execution configured for this application")
        return resolved

    return resolve, close


def _tls_endpoints(spec, databases, engine, *, sides):
    """Each side's registered TLS endpoint (PostgreSQL), keyed by side."""
    from sova.adapters.tls_refusal import TlsEndpoint

    if spec is None:
        return {}
    if engine != "postgresql" or type(spec) is not dict or set(spec) - set(databases):
        raise ContractError("tls_endpoints: PostgreSQL databases of this entry only")
    endpoints = {}
    for side, database in sides.items():
        entry = spec.get(database)
        if entry is None:
            continue
        if type(entry) is not dict or set(entry) != {"host", "port", "server_name", "ca_file"}:
            raise ContractError("A TLS endpoint: host, port, server_name and ca_file")
        endpoints[side] = TlsEndpoint(entry["host"], entry["port"], entry["server_name"],
                                      Path(entry["ca_file"]).read_bytes(), preamble="postgres")
    return endpoints


def _replication(spec):
    """MySQL change capture: the source as the target server reaches it, the
    replication account and its password file (read here, never logged)."""
    if spec is None:
        return None
    if type(spec) is not dict or set(spec) - _REPLICATION_KEYS or not {
            "source_endpoint", "account", "password_file"} <= set(spec):
        raise ContractError("replication: source_endpoint, account and password_file")
    endpoint = spec["source_endpoint"]
    if type(endpoint) is not list or len(endpoint) != 2:
        raise ContractError("replication.source_endpoint: [host, port]")
    snapshot = spec.get("snapshot", "locked")
    if snapshot not in ("locked", "lockless"):
        raise ContractError("replication.snapshot: locked (a global read lock for an "
                            "instant) or lockless (managed services)")
    return {"source_endpoint": (endpoint[0], endpoint[1]), "account": spec["account"],
            "password": _read(spec["password_file"]), "tls": bool(spec.get("tls", False)),
            "ca_path": spec.get("ca_path"), "snapshot": snapshot}


def _postgres_connections(databases, passwords):
    import psycopg

    from sova.adapters.postgres_connections import ReusedConnections

    servers = {db: _read(entry["dsn_file"]) for db, entry in databases.items()}
    admin = ReusedConnections(lambda database: psycopg.connect(servers[database],
                                                               dbname=database))

    def connect_as(database, role):
        extra = {"password": _read(passwords[role])} if role in passwords else {}
        return psycopg.connect(servers[database], dbname=database, user=role, **extra)

    return admin, admin.close, connect_as


def _mysql_connections(databases, passwords):
    import pymysql

    servers = {}
    for db, entry in databases.items():
        settings = json.loads(_read(entry["dsn_file"]))
        if type(settings) is not dict or set(settings) - _MYSQL_SETTINGS or not {
                "host", "user"} <= set(settings):
            raise ContractError("MySQL connection settings: host, user and known keys only")
        if settings.get("ssl_ca"):
            # PyMySQL's own arguments: an ``ssl`` dict next to any ``ssl_*`` argument
            # is replaced by one built from them, which would drop the CA file.
            settings["ssl_verify_cert"] = True
            settings["ssl_verify_identity"] = settings.get("ssl_verify_identity", True)
        servers[db] = settings

    def admin(database):
        return pymysql.connect(**servers[database], autocommit=False)

    def connect_as(database, account):
        settings = {k: v for k, v in servers[database].items() if k not in ("user", "password")}
        extra = {"password": _read(passwords[account])} if account in passwords else {}
        return pymysql.connect(**settings, user=account.rsplit("@", 1)[0], database=database,
                               autocommit=False, **extra)

    return admin, lambda: None, connect_as


def _unit_resolver(app, *, route_for, controls_for):
    """A unit whose entry carries its own keys: (factory of its Execution, close).

    Their mode is offline_copy: a file system, in-memory sessions and a
    Redis-protocol keyspace have no change log to follow while the source
    still serves (a snapshot-cdc plan still runs: its sync copies again).
    MongoDB has one: its ``change_stream`` mode follows the source's."""
    from sova.service.composite_execution import (
        composite_resolver,
        session_execution,
        session_resolver,
    )
    from sova.service.volume_execution import controls_from, volume_factory, volume_resolver

    engine = app["engine"]
    modes = _UNIT_MODES.get(engine, {"offline_copy"})
    if set(app) - _UNIT_KEYS - _UNIT_ENGINES[engine] or app.get("mode") not in modes:
        raise ContractError(f"{engine} entry: known keys and mode {' or '.join(sorted(modes))}")
    if engine == "redis":
        from sova.service.redis_execution import redis_factory, redis_resolver

        resolve, close = redis_resolver(app, route_for=route_for)
        return redis_factory(app["adapter_id"], resolve), close
    if engine == "kafka":
        from sova.service.kafka_execution import kafka_factory, kafka_resolver

        resolve, close = kafka_resolver(app, route_for=route_for)
        return kafka_factory(app["adapter_id"], resolve), close
    if engine == "rabbitmq":
        from sova.service.rabbitmq_execution import rabbitmq_factory, rabbitmq_resolver

        resolve, close = rabbitmq_resolver(app, route_for=route_for)
        return rabbitmq_factory(app["adapter_id"], resolve), close
    if engine == "mongodb":
        from sova.service.mongodb_execution import mongodb_factory, mongodb_resolver

        resolve, close = mongodb_resolver(app, route_for=route_for)
        return mongodb_factory(app["adapter_id"], resolve,
                               change_stream=app["mode"] == "change_stream"), close
    controls_for = controls_for or controls_from
    if engine == "kubernetes-volume":
        resolve, close = volume_resolver(app, route_for=route_for, controls_for=controls_for)
        return volume_factory(app["adapter_id"], resolve), close
    if engine == "kubernetes-session":
        settings = session_resolver(app, route_for=route_for, controls_for=controls_for)
        return (lambda *args: session_execution(app["adapter_id"], settings)), lambda: None
    return composite_resolver(app, route_for=route_for, controls_for=controls_for), \
        lambda: None


def _secret_access(spec):
    from sova.service.secret_access import secret_access_probes

    return secret_access_probes(spec)


def _network_reach(spec):
    from sova.service.network_reach import network_reach_check

    return network_reach_check(spec)


def catalog_from(document, *, route_for=kubernetes_route, controls_for=None,
                 secret_access_for=_secret_access, network_reach_for=_network_reach):
    """(AdapterCatalog, close) from a parsed execution file. Any entry may add
    target controls measured beside the unit's own probes: ``secret_access``
    (``sova.service.secret_access``: the application's Secrets against other
    workloads) and ``network_reach`` (``sova.service.network_reach``: its data
    endpoint against other namespaces)."""
    from sova.service.network_reach import CONTROL as NETWORK
    from sova.service.secret_access import CONTROL as SECRETS

    builders = {"secret_access": (SECRETS, lambda spec: secret_access_for(
                    spec).cross_workload_secret_denied),
                "network_reach": (NETWORK, lambda spec: network_reach_for(spec).measure)}
    if type(document) is not dict or document.get("version") != 1 or set(document) - {
            "version", "applications"}:
        raise ContractError("Execution file version 1 required")
    apps = document.get("applications")
    if type(apps) is not list or not apps:
        raise ContractError("At least one application is required")
    by_adapter, kept, target_controls = {}, [], {}
    for app in apps:
        if type(app) is dict and set(app) & set(builders):
            app = dict(app)
            key = (app.get("tenant_id"), app.get("application_id"))
            for name in sorted(set(app) & set(builders)):
                control, build = builders[name]
                target_controls.setdefault(key, {})[control] = build(app.pop(name))
        engine = app.get("engine") if type(app) is dict else None
        if engine in _UNIT_ENGINES:
            resolve, close = _unit_resolver(app, route_for=route_for,
                                                  controls_for=controls_for)
        else:
            resolve, close = application_resolver(app, route_for=route_for)
        kept.append(close)
        key = (app["tenant_id"], app["application_id"])
        kind = (app.get("engine", "postgresql"), app["mode"])
        entry = by_adapter.setdefault(app["adapter_id"], {"kind": kind, "apps": {}})
        if entry["kind"] != kind:
            raise ContractError("One adapter ID, one engine and mode")
        if key in entry["apps"]:
            raise ContractError("An application is configured twice")
        entry["apps"][key] = resolve

    def factory_of(adapter_id, entry):
        if entry["kind"][0] in _UNIT_ENGINES:  # each application's own factory
            def build(tenant_id, application_id, revision):
                found = entry["apps"].get((tenant_id, application_id))
                if found is None:
                    raise ContractError("No execution configured for this application")
                return found(tenant_id, application_id, revision)
            return build

        def resolve(tenant_id, application_id):
            found = entry["apps"].get((tenant_id, application_id))
            if found is None:
                raise ContractError("No execution configured for this application")
            return found(tenant_id, application_id)
        engine, mode = entry["kind"]
        if engine == "mysql":
            from sova.service.mysql_execution import mysql_factory

            return mysql_factory(adapter_id, resolve, replication=mode == "replication")
        from sova.service.postgres_execution import postgres_factory

        return postgres_factory(adapter_id, resolve, cdc=MODES[mode])

    def with_controls(factory):
        from sova.service.secret_access import with_target_controls

        def build(tenant_id, application_id, revision):
            controls = target_controls.get((tenant_id, application_id))
            if not controls:
                return factory(tenant_id, application_id, revision)
            return with_target_controls(factory, controls)(tenant_id, application_id,
                                                           revision)
        return build

    catalog = AdapterCatalog({adapter_id: with_controls(factory_of(adapter_id, entry))
                              for adapter_id, entry in by_adapter.items()})

    def close():
        for each in kept:
            each()

    return catalog, close
