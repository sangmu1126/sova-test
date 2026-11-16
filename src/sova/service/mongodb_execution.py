"""Execution of MongoDB database handoffs for the worker: the database adapter
and its independent probes, measured like any data store (``data_measure``),
a business invariant being a registered check of the target's documents.

An execution-file entry (``engine: mongodb``)::

    {"tenant_id": "...", "application_id": "...", "adapter_id": "...",
     "engine": "mongodb", "mode": "offline_copy",     # or change_stream
     "mongodb": {"servers": {"source": "<settings file>", "target": "<settings file>"},
                 "databases": {"source": "orders", "target": "orders"},
                 "collections": ["orders", "idempotency_results"],
                 "writers": ["orders-app@admin", "orders-batch@admin"],
                 "app_user": "orders-app@admin", "admin_user": "sova-admin@admin",
                 "user_password_files": {"orders-app@admin": "...", ...},
                 "invariants": {"ORDER-01": {"collection": "orders",
                                             "unique": ["customer", "idempotency_key"]}},
                 "scratch_db": "sova_restore", "other_db": "shop",
                 "zone_of": {"target": "<zone>"},
                 "catch_up_seconds": 120},                    # change_stream only
     "route": {...}}

``change_stream`` moves the documents by snapshot and the source's change
stream (the source a replica set): the write stop no longer grows with the
data. ``catch_up_seconds`` bounds the wait for the last changes once the
source is fenced.

A settings file is JSON: ``host``, ``port``, ``username``, ``password``,
``authSource``, and for TLS ``tls_ca`` (the server's certificate and host name
are then verified).
"""

import json
from pathlib import Path

from sova.adapters.mongodb_change_stream import MongoChangeStreamHandoffAdapter
from sova.adapters.mongodb_handoff import MongoDatabaseHandoffAdapter, account
from sova.adapters.mongodb_probes import MongoDatabaseProbes
from sova.domain.contracts import ContractError
from sova.service.postgres_execution import data_measure
from sova.service.worker import Execution

_KEYS = {"servers", "databases", "collections", "writers", "app_user", "admin_user",
         "user_password_files", "invariants", "scratch_db", "other_db", "zone_of",
         "catch_up_seconds"}
MODES = frozenset({"offline_copy", "change_stream"})
_SERVER_KEYS = {"host", "port", "username", "password", "authSource", "tls_ca"}


def mongodb_measure(probes, *, scratch, other_db):
    base = data_measure(probes, other_db=other_db, scratch_db=scratch)

    def measure(obligation, times):
        if obligation["kind"] == "business_invariant":
            return probes.invariant(obligation["id"].split(".", 1)[1])
        return base(obligation, times)
    return measure


def mongodb_factory(adapter_id, resolve, *, change_stream=False):
    def factory(tenant_id, application_id, revision):
        d = resolve(tenant_id, application_id)
        common = dict(connect=d["connect"], source_db=d["databases"]["source"],
                      target_db=d["databases"]["target"], collections=d["collections"],
                      writers=d["writers"], app_user=d["app_user"],
                      admin_user=d["admin_user"], route=d["route"])
        adapter = MongoChangeStreamHandoffAdapter(
            adapter_id, catch_up_seconds=d.get("catch_up_seconds", 120), **common) \
            if change_stream else MongoDatabaseHandoffAdapter(adapter_id, **common)
        probes = MongoDatabaseProbes(
            connect=d["connect"], connect_as=d["connect_as"], databases=d["databases"],
            collections=d["collections"], writers=d["writers"], app_user=d["app_user"],
            invariants=d.get("invariants"), other_db=d.get("other_db"),
            zone_of=d.get("zone_of"))
        return Execution(adapter, mongodb_measure(probes, scratch=d.get("scratch_db"),
                                                  other_db=d.get("other_db")))
    return factory


def _read(path, limit=65536):
    data = Path(path).read_bytes()
    if not data.strip() or len(data) > limit:
        raise ContractError(f"Unreadable configuration file {path}")
    return data.decode("utf-8").strip()


def _client_settings(path):
    settings = json.loads(_read(path))
    if type(settings) is not dict or set(settings) - _SERVER_KEYS or "host" not in settings:
        raise ContractError("MongoDB server settings: host and known keys only")
    ca = settings.pop("tls_ca", None)
    if ca:  # verification of certificate and host name stays on (the driver's default)
        settings.update(tls=True, tlsCAFile=ca)
    return settings


def mongodb_resolver(app, *, route_for):
    import pymongo

    entry = app.get("mongodb")
    if type(entry) is not dict or set(entry) - _KEYS or not {
            "servers", "databases", "collections", "writers", "app_user",
            "admin_user"} <= set(entry):
        raise ContractError("MongoDB execution entry has missing or unknown keys")
    servers, databases = entry["servers"], entry["databases"]
    if type(servers) is not dict or set(servers) != {"source", "target"} or \
            type(databases) is not dict or set(databases) != {"source", "target"}:
        raise ContractError("servers and databases: source and target")
    settings = {side: _client_settings(path) for side, path in servers.items()}
    passwords = entry.get("user_password_files") or {}
    zones = entry.get("zone_of") or {}
    if type(zones) is not dict or set(zones) - {"source", "target"}:
        raise ContractError("zone_of: source and target labels")
    clients = {}

    def connect(side):
        if side not in clients:
            clients[side] = pymongo.MongoClient(**settings[side], serverSelectionTimeoutMS=10000)
        return clients[side]

    def connect_as(side, writer):
        user, authdb = account(writer)
        login = {k: v for k, v in settings[side].items()
                 if k not in ("username", "password", "authSource")}
        return pymongo.MongoClient(**login, username=user,
                                   password=_read(passwords[writer]) if writer in passwords
                                   else "", authSource=authdb, serverSelectionTimeoutMS=10000)

    resolved = {
        "connect": connect, "connect_as": connect_as, "databases": dict(databases),
        "collections": tuple(entry["collections"]), "writers": tuple(entry["writers"]),
        "app_user": entry["app_user"], "admin_user": entry["admin_user"],
        "route": route_for(app["route"]), "invariants": entry.get("invariants"),
        "scratch_db": entry.get("scratch_db", "sova_restore"),
        "other_db": entry.get("other_db"),
        "catch_up_seconds": entry.get("catch_up_seconds", 120),
        "zone_of": (lambda side: zones.get(side)) if zones else None}

    def resolve(tenant_id, application_id):
        if (tenant_id, application_id) != (app["tenant_id"], app["application_id"]):
            raise ContractError("No execution configured for this application")
        return resolved

    def close():
        for client in clients.values():
            client.close()
    return resolve, close
