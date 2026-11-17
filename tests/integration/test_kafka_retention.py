"""The source keeps deleting its oldest records by time after they were
copied; the target copy does not with it (its segments are new, their newest
record recent). Those records are left out of the comparison and counted -
when the source's retention can have removed them - instead of holding a
correct handoff; a target missing records the source has is never equal."""

from datetime import UTC, datetime
import time

from adapter_conformance import apply, command, readback
from kafka_world import KafkaWorld
import pytest

from sova.adapters.kafka_topic_probes import KafkaTopicProbes
from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.kafka
RETENTION_MS = 10_000  # closed segments go; Redpanda may judge by its own receipt time


def probes(b):
    by_principal = {p: key for key, p in b["principals"].items()}
    return KafkaTopicProbes(
        config=lambda side: dict(b["settings"]),
        config_as=lambda side, principal: b["as_user"](by_principal[principal]),
        topics={"source": b["names"]["src"], "target": b["names"]["tgt"]},
        producers=(b["principals"]["app"], b["principals"]["batch"]),
        app_producer=b["principals"]["app"], probe_prefix=b["probe"], groups=(b["group"],),
        sets={"orders": b["names"]["src"]})


def start(b, topic, partition):
    tp = b["TopicPartition"](topic, partition)
    from confluent_kafka.admin import OffsetSpec

    return b["admin"].list_offsets({tp: OffsetSpec.earliest()},
                                   request_timeout=15)[tp].result().offset


def test_records_the_source_expires_after_the_copy_do_not_hold_the_handoff(
        kafka_retained_business):
    from confluent_kafka.admin import AlterConfigOpType, ConfigEntry, ConfigResource
    from confluent_kafka.admin import ResourceType

    b = kafka_retained_business
    world = KafkaWorld(b)
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "copy_content") == EffectStatus.APPLIED
    # The operator's retention, or time passing: the source's old segment goes.
    resource = ConfigResource(ResourceType.TOPIC, b["names"]["src"], incremental_configs=[
        ConfigEntry("retention.ms", str(RETENTION_MS), incremental_operation=AlterConfigOpType.SET)])
    for future in b["admin"].incremental_alter_configs([resource]).values():
        future.result()
    deadline = time.monotonic() + 180
    while start(b, b["names"]["src"], 0) == 0:
        assert time.monotonic() < deadline, "the source kept its old segment"
        time.sleep(2)
    assert start(b, b["names"]["tgt"], 0) == 0  # the target keeps them (7 days)
    seen = world.adapter().readback(command("kafka-topic-handoff", "copy_content"),
                                    datetime.now(UTC)).evidence
    assert readback(world.adapter(), "copy_content") == EffectStatus.APPLIED, seen
    # At least the old segment; with 10 s, the business records may go too.
    dropped = seen["expired_on_source_only"]
    assert dropped.get("0", 0) >= 1 and set(dropped) <= {"0", "1", "2"}, dropped
    passed, observed = probes(b).content_equal("orders")
    assert passed and observed["expired_on_source_only"].get("0", 0) >= 1, observed
    # A record no retention explains - the target holds one the source never
    # had, produced now - is never left out.
    from confluent_kafka import Producer

    producer = Producer(dict(b["settings"]))
    producer.produce(b["names"]["tgt"], key=b"forged", value=b"x", partition=2)
    producer.flush(15)
    assert readback(world.adapter(), "copy_content") != EffectStatus.APPLIED
    assert not probes(b).content_equal("orders")[0]
