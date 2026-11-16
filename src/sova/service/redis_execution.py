"""Execution of Redis-protocol keyspace handoffs for the worker: the keyspace
adapter and its independent probes, measured like any data store
(``data_measure``), a business invariant being a registered reference check.

An execution-file entry (``engine: redis``)::

    {"tenant_id": "...", "application_id": "...", "adapter_id": "...",
     "engine": "redis", "mode": "offline_copy",
     "redis": {"servers": {"source": "<settings file>", "target": "<settings file>"},
               "prefixes": ["board:"], "writers": ["board-app", "board-batch"],
               "app_user": "board-app", "admin_user": "sova-admin",
               "user_password_files": {"board-app": "...", "board-batch": "..."},
               "sets": {"sessions": "board:session:", "carts": "board:cart:"},
               "invariants": {"CART-01": {"from": "board:session:", "field": "cart",
                                          "to": "board:cart:"}},
               "scratch_db": 15, "other_prefix": "shop:", "routes": ["src", "tgt"],
               "zone_of": {"source": "<zone>", "target": "<zone>"}},
     "route": {...}}

A server settings file is JSON: ``host``, ``port``, ``username``, ``password``,
``db``, and for TLS ``ssl_ca`` (the server certificate and host name are then
verified).
"""

import json
from pathlib import Path

from sova.adapters.redis_keyspace_handoff import RedisKeyspaceHandoffAdapter
from sova.adapters.redis_keyspace_probes import RedisKeyspaceProbes
from sova.domain.contracts import ContractError
from sova.service.postgres_execution import data_measure
from sova.service.worker import Execution

_KEYS = {"servers", "prefixes", "writers", "app_user", "admin_user", "user_password_files",
         "sets", "invariants", "scratch_db", "other_prefix", "routes", "zone_of"}
_SERVER_KEYS = {"host", "port", "username", "password", "db", "ssl_ca", "socket_timeout"}


def redis_measure(probes, *, scratch_db, other_prefix):
    base = data_measure(probes, other_db=other_prefix, scratch_db=scratch_db)

    def measure(obligation, times):
        if obligation["kind"] == "business_invariant":
            return probes.invariant(obligation["id"].split(".", 1)[1])
        return base(obligation, times)
    return measure


def redis_factory(adapter_id, resolve):
    def factory(tenant_id, application_id, revision):
        d = resolve(tenant_id, application_id)
        adapter = RedisKeyspaceHandoffAdapter(
            adapter_id, connect=d["connect"], prefixes=d["prefixes"], writers=d["writers"],
            app_user=d["app_user"], admin_user=d["admin_user"], route=d["route"],
            routes=d["routes"])
        probes = RedisKeyspaceProbes(
            connect=d["connect"], connect_as=d["connect_as"], writers=d["writers"],
            app_user=d["app_user"], sets=d["sets"], invariants=d.get("invariants"),
            scratch_db=d.get("scratch_db", 15), other_prefix=d.get("other_prefix"),
            zone_of=d.get("zone_of"))
        return Execution(adapter, redis_measure(probes, scratch_db=d.get("scratch_db", 15),
                                                other_prefix=d.get("other_prefix")))
    return factory


def _read(path, limit=65536):
    data = Path(path).read_bytes()
    if not data.strip() or len(data) > limit:
        raise ContractError(f"Unreadable configuration file {path}")
    return data.decode("utf-8").strip()


def _server(path):
    settings = json.loads(_read(path))
    if type(settings) is not dict or set(settings) - _SERVER_KEYS or "host" not in settings:
        raise ContractError("Redis server settings: host and known keys only")
    ca = settings.pop("ssl_ca", None)
    if ca:
        settings.update(ssl=True, ssl_ca_certs=ca, ssl_cert_reqs="required",
                        ssl_check_hostname=True)
    return settings


def redis_resolver(app, *, route_for):
    import redis

    entry = app.get("redis")
    if type(entry) is not dict or set(entry) - _KEYS or not {
            "servers", "prefixes", "writers", "app_user", "admin_user", "sets",
            "routes"} <= set(entry):
        raise ContractError("Redis execution entry has missing or unknown keys")
    servers = entry["servers"]
    if type(servers) is not dict or set(servers) != {"source", "target"}:
        raise ContractError("servers: a settings file for source and target")
    settings = {side: _server(path) for side, path in servers.items()}
    passwords = entry.get("user_password_files") or {}
    routes = entry["routes"]
    if type(routes) is not list or len(routes) != 2:
        raise ContractError("routes: [source value, target value] required")
    zones = entry.get("zone_of") or {}
    if type(zones) is not dict or set(zones) - {"source", "target"}:
        raise ContractError("zone_of: source and target labels")
    clients = {}

    def connect(side):
        if side not in clients:
            clients[side] = redis.Redis(**settings[side])
        return clients[side]

    def connect_as(side, name):
        login = {k: v for k, v in settings[side].items() if k not in ("username", "password")}
        password = _read(passwords[name]) if name in passwords else None
        return redis.Redis(**login, username=name, password=password)

    resolved = {
        "connect": connect, "connect_as": connect_as, "prefixes": tuple(entry["prefixes"]),
        "writers": tuple(entry["writers"]), "app_user": entry["app_user"],
        "admin_user": entry["admin_user"], "route": route_for(app["route"]),
        "routes": tuple(routes), "sets": entry["sets"], "invariants": entry.get("invariants"),
        "scratch_db": entry.get("scratch_db", 15), "other_prefix": entry.get("other_prefix"),
        "zone_of": (lambda side: zones.get(side)) if zones else None}

    def resolve(tenant_id, application_id):
        if (tenant_id, application_id) != (app["tenant_id"], app["application_id"]):
            raise ContractError("No execution configured for this application")
        return resolved

    def close():
        for client in clients.values():
            client.close()
    return resolve, close
