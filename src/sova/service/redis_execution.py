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
               "zone_of": {"source": "<zone>", "target": "<zone>"},
               "replicas": {"source": ["<settings file>"], "target": []},
               "sentinels": {"source": {"servers": ["<settings file>"],
                                        "master": "orders"}},
               "expiry_margin_seconds": 120},
     "route": {...}}

``expiry_margin_seconds`` (default 120) is how close to its expiry a key or
hash field is left out of the comparisons, on both sides: longer than a
comparison of the whole unit takes, plus the servers' clock skew.

``replicas`` registers each side's replicas (a settings file each, reached
directly): ACL users are kept per node, so the fence and activation go to
every node, and a primary that reports a replica not registered here keeps
the handoff from confirming them. ``sentinels`` names, per side managed by
Sentinel, its Sentinels and the master's name there: every Sentinel must
name the side's primary as the master and know exactly the registered
replicas - including one cut off from the primary, which the primary no
longer counts.

A server settings file is JSON: ``host``, ``port``, ``username``, ``password``,
``db``, and for TLS ``ssl_ca`` (the server certificate and host name are then
verified). ``"cluster": true`` makes the side a cluster: ``host``/``port`` name a
node to start from, every node the cluster lists is a node of the side, and a
writer's own login is made on one node at a time (it need not be allowed to
read the cluster's topology).
"""

import json
from pathlib import Path

from sova.adapters.redis_keyspace_handoff import RedisKeyspaceHandoffAdapter
from sova.adapters.redis_keyspace_probes import RedisKeyspaceProbes
from sova.domain.contracts import ContractError
from sova.service.postgres_execution import data_measure
from sova.service.worker import Execution

_KEYS = {"servers", "prefixes", "writers", "app_user", "admin_user", "user_password_files",
         "sets", "invariants", "scratch_db", "other_prefix", "routes", "zone_of", "replicas",
         "sentinels", "expiry_margin_seconds"}
_SERVER_KEYS = {"host", "port", "username", "password", "db", "ssl_ca", "socket_timeout",
                "cluster"}


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
            routes=d["routes"], replicas=d.get("replicas"), sentinel=d.get("sentinel"),
            expiry_margin_seconds=d.get("expiry_margin_seconds", 120))
        probes = RedisKeyspaceProbes(
            connect=d["connect"], connect_as=d["connect_as"], writers=d["writers"],
            app_user=d["app_user"], sets=d["sets"], invariants=d.get("invariants"),
            scratch_db=d.get("scratch_db", 15), other_prefix=d.get("other_prefix"),
            zone_of=d.get("zone_of"), replicas=d.get("replicas"),
            expiry_margin_seconds=d.get("expiry_margin_seconds", 120))
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
    listed = entry.get("replicas") or {}
    if type(listed) is not dict or set(listed) - {"source", "target"} or not all(
            type(v) is list for v in listed.values()):
        raise ContractError("replicas: a list of settings files per side")
    replica_settings = {side: [_server(path) for path in listed.get(side, [])]
                        for side in ("source", "target")}
    watched = entry.get("sentinels") or {}
    if type(watched) is not dict or set(watched) - {"source", "target"} or not all(
            type(v) is dict and set(v) == {"servers", "master"} and type(v["servers"]) is list
            and v["servers"] and type(v["master"]) is str and v["master"]
            for v in watched.values()):
        raise ContractError("sentinels: per side, servers (settings files) and master")
    sentinel_settings = {side: ([_server(path) for path in v["servers"]], v["master"])
                         for side, v in watched.items()}
    passwords = entry.get("user_password_files") or {}
    routes = entry["routes"]
    if type(routes) is not list or len(routes) != 2:
        raise ContractError("routes: [source value, target value] required")
    zones = entry.get("zone_of") or {}
    if type(zones) is not dict or set(zones) - {"source", "target"}:
        raise ContractError("zone_of: source and target labels")
    clients, replica_clients = {}, {}

    def connect(side):
        if side not in clients:
            s = {k: v for k, v in settings[side].items() if k != "cluster"}
            clients[side] = redis.cluster.RedisCluster(**s) if settings[side].get("cluster") \
                else redis.Redis(**s)
        return clients[side]

    def replicas(side):
        if side not in replica_clients:
            replica_clients[side] = [redis.Redis(**s) for s in replica_settings[side]]
        return replica_clients[side]

    def sentinel(side):
        if side not in sentinel_settings:
            return None
        if ("sentinel", side) not in replica_clients:
            servers, _ = sentinel_settings[side]
            replica_clients[("sentinel", side)] = [redis.Redis(**s) for s in servers]
        return replica_clients[("sentinel", side)], sentinel_settings[side][1]

    def connect_as(side, name, node=None):
        """A writer's own login, tried once: redis-py retries a ConnectionError -
        a refused login (AuthenticationError) is one - about ten times with
        growing waits by default, so each refused probe attempt took seconds.
        On a cluster, ``node`` is the ``(host, port)`` to log in to."""
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        login = {k: v for k, v in settings[side].items()
                 if k not in ("username", "password", "cluster")}
        if node is not None:
            login.update(host=node[0], port=node[1])
        password = _read(passwords[name]) if name in passwords else None
        return redis.Redis(**login, username=name, password=password,
                           retry=Retry(NoBackoff(), 0))

    resolved = {
        "connect": connect, "connect_as": connect_as, "prefixes": tuple(entry["prefixes"]),
        "writers": tuple(entry["writers"]), "app_user": entry["app_user"],
        "admin_user": entry["admin_user"], "route": route_for(app["route"]),
        "routes": tuple(routes), "sets": entry["sets"], "invariants": entry.get("invariants"),
        "scratch_db": entry.get("scratch_db", 15), "other_prefix": entry.get("other_prefix"),
        "replicas": replicas, "sentinel": sentinel,
        "expiry_margin_seconds": entry.get("expiry_margin_seconds", 120),
        "zone_of": (lambda side: zones.get(side)) if zones else None}

    def resolve(tenant_id, application_id):
        if (tenant_id, application_id) != (app["tenant_id"], app["application_id"]):
            raise ContractError("No execution configured for this application")
        return resolved

    def close():
        for client in [*clients.values(), *(c for cs in replica_clients.values() for c in cs)]:
            client.close()
    return resolve, close
