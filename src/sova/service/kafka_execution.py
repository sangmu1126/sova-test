"""Execution of Kafka-API topic handoffs for the worker: the topic adapter and
its independent probes, measured like any data store (``data_measure``), a
business invariant being a registered check of the target's records.

An execution-file entry (``engine: kafka``)::

    {"tenant_id": "...", "application_id": "...", "adapter_id": "...",
     "engine": "kafka", "mode": "offline_copy",
     "kafka": {"clusters": {"source": "<settings file>", "target": "<settings file>"},
               "topics": {"source": "orders", "target": "orders"},
               "producers": ["User:orders-app", "User:orders-batch"],
               "app_producer": "User:orders-app", "consumers": ["User:orders-reader"],
               "groups": ["orders-consumers"], "transactional_prefixes": ["orders-app-tx"],
               "probe_prefix": "sova-probe-orders-",   # each producer may use it
               "principal_settings": {"User:orders-app": {"source": "<file>", "target": "..."}},
               "sets": {"orders": "orders"},
               "invariants": {"ORDER-02": {"type": "json_field", "field": "order"}},
               "other_topic": "shop-events", "zone_of": {"target": "<zone>"}},
     "route": {...}}

A settings file is a JSON object of client settings, only the connection
ones (``bootstrap.servers``, ``security.protocol``, SASL and TLS settings):
a setting that loads code or writes files (``plugin.library.paths``, debug
output) is refused, whoever wrote the file.
"""

import json
from pathlib import Path

from sova.adapters.kafka_topic_handoff import KafkaTopicHandoffAdapter
from sova.adapters.kafka_topic_probes import KafkaTopicProbes
from sova.domain.contracts import ContractError
from sova.service.postgres_execution import data_measure
from sova.service.worker import Execution

_KEYS = {"clusters", "topics", "producers", "app_producer", "consumers", "groups",
         "transactional_prefixes", "principal_settings", "sets", "invariants", "other_topic",
         "zone_of", "probe_prefix"}
SETTINGS = {"bootstrap.servers", "security.protocol", "sasl.mechanism", "sasl.username",
            "sasl.password", "ssl.ca.location", "ssl.endpoint.identification.algorithm",
            "client.id", "socket.timeout.ms", "request.timeout.ms"}


def kafka_measure(probes, *, scratch, other_topic):
    base = data_measure(probes, other_db=other_topic, scratch_db=scratch)

    def measure(obligation, times):
        if obligation["kind"] == "business_invariant":
            return probes.invariant(obligation["id"].split(".", 1)[1])
        return base(obligation, times)
    return measure


def kafka_factory(adapter_id, resolve):
    def factory(tenant_id, application_id, revision):
        d = resolve(tenant_id, application_id)
        adapter = KafkaTopicHandoffAdapter(
            adapter_id, config=d["config"], source_topic=d["topics"]["source"],
            target_topic=d["topics"]["target"], producers=d["producers"],
            app_producer=d["app_producer"], route=d["route"], consumers=d.get("consumers", ()),
            groups=d.get("groups", ()),
            transactional_prefixes=d.get("transactional_prefixes", ()))
        probes = KafkaTopicProbes(
            config=d["config"], config_as=d["config_as"], topics=d["topics"],
            producers=d["producers"], app_producer=d["app_producer"],
            probe_prefix=d["probe_prefix"], groups=d.get("groups", ()), sets=d.get("sets"),
            invariants=d.get("invariants"),
            other_topic=d.get("other_topic"), zone_of=d.get("zone_of"))
        return Execution(adapter, kafka_measure(probes, scratch="sova-restore",
                                                other_topic=d.get("other_topic")))
    return factory


def settings_file(path):
    data = Path(path).read_bytes()
    if not data.strip() or len(data) > 65536:
        raise ContractError(f"Unreadable configuration file {path}")
    settings = json.loads(data)
    if type(settings) is not dict or set(settings) - SETTINGS or \
            "bootstrap.servers" not in settings or not all(
                type(v) in (str, int) for v in settings.values()):
        raise ContractError("Kafka settings: bootstrap.servers and connection settings only")
    return settings


def kafka_resolver(app, *, route_for):
    entry = app.get("kafka")
    if type(entry) is not dict or set(entry) - _KEYS or not {
            "clusters", "topics", "producers", "app_producer", "principal_settings",
            "probe_prefix"} <= set(entry):
        raise ContractError("Kafka execution entry has missing or unknown keys")
    clusters, topics = entry["clusters"], entry["topics"]
    if type(clusters) is not dict or set(clusters) != {"source", "target"} or \
            type(topics) is not dict or set(topics) != {"source", "target"}:
        raise ContractError("clusters and topics: source and target")
    admin = {side: settings_file(path) for side, path in clusters.items()}
    principals = entry["principal_settings"]
    if type(principals) is not dict or set(entry["producers"]) - set(principals):
        raise ContractError("principal_settings: every producer's own settings per side")
    own = {p: {side: settings_file(path) for side, path in sides.items()}
           for p, sides in principals.items()}
    zones = entry.get("zone_of") or {}
    if type(zones) is not dict or set(zones) - {"source", "target"}:
        raise ContractError("zone_of: source and target labels")
    resolved = {
        "config": lambda side: dict(admin[side]),
        "config_as": lambda side, principal: dict(own[principal][side]),
        "topics": dict(topics), "producers": tuple(entry["producers"]),
        "app_producer": entry["app_producer"], "consumers": tuple(entry.get("consumers", ())),
        "groups": tuple(entry.get("groups", ())),
        "transactional_prefixes": tuple(entry.get("transactional_prefixes", ())),
        "sets": entry.get("sets"), "invariants": entry.get("invariants"),
        "other_topic": entry.get("other_topic"), "route": route_for(app["route"]),
        "probe_prefix": entry["probe_prefix"],
        "zone_of": (lambda side: zones.get(side)) if zones else None}

    def resolve(tenant_id, application_id):
        if (tenant_id, application_id) != (app["tenant_id"], app["application_id"]):
            raise ContractError("No execution configured for this application")
        return resolved
    return resolve, lambda: None
