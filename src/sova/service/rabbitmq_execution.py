"""Execution of RabbitMQ queue handoffs for the worker: the queue adapter and
its independent probes, measured like any data store (``data_measure``).

An execution-file entry (``engine: rabbitmq``)::

    {"tenant_id": "...", "application_id": "...", "adapter_id": "...",
     "engine": "rabbitmq", "mode": "offline_copy",
     "rabbitmq": {"brokers": {"source": "<settings file>", "target": "<settings file>"},
                  "vhosts": {"source": "orders", "target": "orders-new"},
                  "queue": "orders-work", "exchange": "orders", "routing_key": "order.created",
                  "publishers": ["orders-app", "orders-batch"], "app_publisher": "orders-app",
                  "consumers": ["orders-reader"],
                  "user_password_files": {"orders-app": "...", "orders-batch": "..."},
                  "invariants": {"QUEUE-01": {"type": "same_count"}},
                  "other_vhost": "shop", "zone_of": {"target": "<zone>"}},
     "route": {...}}

A broker settings file is JSON: the management API ``http`` base, the AMQP
``host`` and ``port``, the administrative ``username`` and ``password``, and
for TLS ``ssl_ca`` (the broker's certificate and host name are then verified).
"""

import json
from pathlib import Path
import ssl

from sova.adapters.rabbitmq_queue_handoff import RabbitMqQueueHandoffAdapter
from sova.adapters.rabbitmq_queue_probes import RabbitMqQueueProbes
from sova.domain.contracts import ContractError
from sova.service.postgres_execution import data_measure
from sova.service.worker import Execution

_KEYS = {"brokers", "vhosts", "queue", "exchange", "routing_key", "publishers",
         "app_publisher", "consumers", "user_password_files", "invariants", "other_vhost",
         "zone_of"}
_BROKER_KEYS = {"http", "host", "port", "username", "password", "ssl_ca"}


def rabbitmq_measure(probes, *, other_vhost):
    base = data_measure(probes, other_db=other_vhost, scratch_db="sova-restore")

    def measure(obligation, times):
        if obligation["kind"] == "business_invariant":
            return probes.invariant(obligation["id"].split(".", 1)[1])
        return base(obligation, times)
    return measure


def rabbitmq_factory(adapter_id, resolve):
    def factory(tenant_id, application_id, revision):
        d = resolve(tenant_id, application_id)
        adapter = RabbitMqQueueHandoffAdapter(
            adapter_id, broker=d["broker"], source_vhost=d["vhosts"]["source"],
            target_vhost=d["vhosts"]["target"], queue=d["queue"], exchange=d["exchange"],
            routing_key=d["routing_key"], publishers=d["publishers"],
            app_publisher=d["app_publisher"], consumers=d.get("consumers", ()),
            route=d["route"], admin_user=d["admin_user"])
        probes = RabbitMqQueueProbes(
            broker=d["broker"], connect_as=d["connect_as"], vhosts=d["vhosts"],
            queue=d["queue"], exchange=d["exchange"], routing_key=d["routing_key"],
            publishers=d["publishers"], app_publisher=d["app_publisher"],
            invariants=d.get("invariants"), other_vhost=d.get("other_vhost"),
            zone_of=d.get("zone_of"))
        return Execution(adapter, rabbitmq_measure(probes, other_vhost=d.get("other_vhost")))
    return factory


def _read(path, limit=65536):
    data = Path(path).read_bytes()
    if not data.strip() or len(data) > limit:
        raise ContractError(f"Unreadable configuration file {path}")
    return data.decode("utf-8").strip()


def rabbitmq_resolver(app, *, route_for):
    import pika

    entry = app.get("rabbitmq")
    if type(entry) is not dict or set(entry) - _KEYS or not {
            "brokers", "vhosts", "queue", "exchange", "routing_key", "publishers",
            "app_publisher"} <= set(entry):
        raise ContractError("RabbitMQ execution entry has missing or unknown keys")
    brokers, vhosts = entry["brokers"], entry["vhosts"]
    if type(brokers) is not dict or set(brokers) != {"source", "target"} or \
            type(vhosts) is not dict or set(vhosts) != {"source", "target"}:
        raise ContractError("brokers and vhosts: source and target")
    settings = {}
    for side, path in brokers.items():
        found = json.loads(_read(path))
        if type(found) is not dict or set(found) - _BROKER_KEYS or not {
                "http", "host", "username", "password"} <= set(found):
            raise ContractError("RabbitMQ broker settings: http, host and credentials")
        settings[side] = found
    passwords = entry.get("user_password_files") or {}
    zones = entry.get("zone_of") or {}
    if type(zones) is not dict or set(zones) - {"source", "target"}:
        raise ContractError("zone_of: source and target labels")
    admin_user = {s["username"] for s in settings.values()}
    if len(admin_user) != 1:
        raise ContractError("One administrative user on both brokers")

    def parameters(side, vhost, credentials):
        s = settings[side]
        options = None
        if s.get("ssl_ca"):
            context = ssl.create_default_context(cafile=s["ssl_ca"])
            options = pika.SSLOptions(context, s["host"])
        return pika.ConnectionParameters(s["host"], s.get("port", 5672), vhost,
                                         pika.PlainCredentials(*credentials),
                                         ssl_options=options, connection_attempts=1,
                                         socket_timeout=5, blocked_connection_timeout=10)

    def broker(side):
        s = settings[side]
        return {"http": s["http"], "auth": (s["username"], s["password"]),
                "amqp": lambda vhost: parameters(side, vhost, (s["username"], s["password"]))}

    def connect_as(side, user, vhost):
        return parameters(side, vhost, (user, _read(passwords[user]) if user in passwords
                                        else ""))

    resolved = {
        "broker": broker, "connect_as": connect_as, "vhosts": dict(vhosts),
        "queue": entry["queue"], "exchange": entry["exchange"],
        "routing_key": entry["routing_key"], "publishers": tuple(entry["publishers"]),
        "app_publisher": entry["app_publisher"], "consumers": tuple(entry.get("consumers", ())),
        "admin_user": admin_user.pop(), "route": route_for(app["route"]),
        "other_vhost": entry.get("other_vhost"), "invariants": entry.get("invariants"),
        "zone_of": (lambda side: zones.get(side)) if zones else None}

    def resolve(tenant_id, application_id):
        if (tenant_id, application_id) != (app["tenant_id"], app["application_id"]):
            raise ContractError("No execution configured for this application")
        return resolved
    return resolve, lambda: None
