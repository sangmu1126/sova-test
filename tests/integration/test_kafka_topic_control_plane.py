"""A Kafka-API topic (the order events) handed off by the whole control plane,
and the topic probes failing where they must. On Redpanda with SASL/SCRAM and
ACLs; another tenant's topic sits on the same cluster."""

import json

from confluent_kafka import Producer
from confluent_kafka.admin import NewTopic
from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import without_transport_control
from kafka_world import Route
import pytest

from sova.adapters.kafka_topic_probes import KafkaTopicProbes
from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.service.kafka_execution import kafka_factory

pytestmark = [pytest.mark.postgres, pytest.mark.kafka]
EVENTS = {"id": "ORDER-02", "description": "모든 주문 이벤트는 주문 번호를 가진 JSON이다."}


@pytest.fixture
def other_topic(kafka_business):
    """Another tenant's topic on the same cluster, with a record in it."""
    name = kafka_business["names"]["src"].replace("orders-", "shop-")
    admin = kafka_business["admin"]
    admin.create_topics([NewTopic(name, 1, 1)])[name].result()
    producer = Producer(kafka_business["settings"])
    producer.produce(name, value=b'{"shop": true}', partition=0)
    producer.flush(15)
    yield name
    admin.delete_topics([name])


def settings_of(b, other):
    by_principal = {p: key for key, p in b["principals"].items()}
    return {
        "config": lambda side: dict(b["settings"]),
        "config_as": lambda side, principal: b["as_user"](by_principal[principal]),
        "topics": {"source": b["names"]["src"], "target": b["names"]["tgt"]},
        "producers": (b["principals"]["app"], b["principals"]["batch"]),
        "app_producer": b["principals"]["app"], "consumers": (b["principals"]["reader"],),
        "groups": (b["group"],), "transactional_prefixes": (b["prefix"],),
        "sets": {"orders": b["names"]["src"]},
        "invariants": {"ORDER-02": {"type": "json_field", "field": "order"}},
        "other_topic": other, "zone_of": lambda side: "approved-zone-a",
        "probe_prefix": b["probe"]}


def test_a_topic_is_handed_off_by_the_control_plane(postgres_database, now, kafka_business,
                                                    other_topic, tmp_path):
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + kafka_business["names"]["src"][7:15] * 4
    data["spec"]["data"]["protected_sets"] = ["orders"]
    data["spec"]["business"]["invariants"] = [EVENTS]
    without_transport_control(data)  # the disposable broker speaks SASL without TLS
    h = prepared(postgres_database, now, tmp_path, data, engine="kafka")
    route = Route(kafka_business["names"]["src"])
    settings = {**settings_of(kafka_business, other_topic), "route": route}
    worker = worker_for(h, kafka_factory(h["adapter_id"], lambda t, a: settings),
                        owner="worker-kafka")
    record = worker.drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert route.get() == kafka_business["names"]["tgt"]
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    seen = h["observations"]
    assert seen["security.deny_cross_tenant_data_access"]["read"] != "record_read"
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith(("availability.", "operations."))}))


def test_each_topic_probe_fails_on_what_it_measures(kafka_business, other_topic):
    from adapter_conformance import apply
    from kafka_world import KafkaWorld

    world = KafkaWorld(kafka_business)
    p = KafkaTopicProbes(**{k: v for k, v in settings_of(kafka_business, other_topic).items()
                            if k not in ("consumers", "transactional_prefixes")})
    passed, live = p.write_denied("source")
    assert not passed and {o["attempt"] for o in live["outcomes"].values()} == {"write_accepted"}
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
                   "activate_target"):
        apply(world.adapter(), action)
    for check in (lambda: p.write_denied("source"), p.single_writer,
                  lambda: p.content_equal("orders"), lambda: p.invariant("ORDER-02"),
                  p.cross_tenant_denied, lambda: p.restore_drill("sova-restore")):
        passed, observation = check()
        assert passed, observation
    world.tamper_target()  # a forged record on the target
    assert not p.content_equal("orders")[0]
    passed, invariant = p.invariant("ORDER-02")
    assert not passed and invariant["violations"]
    assert not p.transport_encrypted("target")[0]


def test_a_writer_whose_probe_cannot_run_is_never_called_denied(kafka_business, other_topic):
    """Its probe transaction refused, the batch job can still write the topic: the
    broker's own authorization view keeps the probe from calling it denied."""
    from confluent_kafka.admin import (
        AclBindingFilter,
        AclOperation,
        AclPermissionType,
        ResourcePatternType,
        ResourceType,
    )

    b = kafka_business
    for future in b["admin"].delete_acls([AclBindingFilter(
            ResourceType.TRANSACTIONAL_ID, b["probe"], ResourcePatternType.PREFIXED,
            b["principals"]["batch"], None, AclOperation.ANY,
            AclPermissionType.ANY)]).values():
        future.result()
    p = KafkaTopicProbes(**{k: v for k, v in settings_of(b, other_topic).items()
                            if k not in ("consumers", "transactional_prefixes")})
    passed, observation = p.write_denied("source")
    batch = observation["outcomes"][b["principals"]["batch"]]
    assert not passed and batch["attempt"].startswith("undecided:"), observation
    assert "WRITE" in batch["authorized"]
