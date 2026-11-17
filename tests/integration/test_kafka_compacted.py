"""A compact-only topic (a state topic: the latest value of each order) handed
off while compaction runs. Each cluster compacts on its own schedule, so the
record sequences differ while their meaning does not: content and group
positions are compared as the latest record of each key; a target filled
again is recreated (a compact-only topic refuses DeleteRecords)."""

import time

from adapter_conformance import apply, readback
from confluent_kafka import Consumer, KafkaError, Producer
from confluent_kafka.admin import OffsetSpec
from kafka_world import KafkaWorld
import pytest

from sova.adapters.kafka_topic_probes import KafkaTopicProbes
from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.kafka


def compacted_away(b, topic):
    """How many records compaction has removed: offsets the log spans that no
    record holds any longer."""
    ends = []
    for spec in (OffsetSpec.earliest(), OffsetSpec.latest()):
        found = b["admin"].list_offsets({b["TopicPartition"](topic, p): spec for p in range(3)},
                                        request_timeout=15)
        ends.append({tp.partition: f.result().offset for tp, f in found.items()})
    span = sum(ends[1][p] - ends[0][p] for p in range(3))
    consumer = Consumer({**b["settings"], "group.id": f"sova-count-{time.monotonic_ns()}",
                         "enable.auto.commit": False, "auto.offset.reset": "earliest",
                         "enable.partition.eof": True})
    consumer.subscribe([topic])
    count, ended, idle = 0, set(), 0
    while len(ended) < 3 and idle < 20:
        message = consumer.poll(1)
        if message is None:
            idle += 1
        elif message.error():
            if message.error().code() == KafkaError._PARTITION_EOF:
                ended.add(message.partition())
        else:
            count += 1
    consumer.close()
    return span - count


def latest(b, topic, group=None):
    """What a reader of ``topic`` ends with: the latest value of each key."""
    settings = {**(b["as_user"]("reader") if group else b["settings"]),
                "group.id": group or f"sova-check-{time.monotonic_ns()}",
                "enable.auto.commit": False, "isolation.level": "read_committed",
                "auto.offset.reset": "earliest", "enable.partition.eof": True}
    consumer = Consumer(settings)
    consumer.subscribe([topic])
    state, ended, idle = {}, set(), 0
    while len(ended) < 3 and idle < 20:
        message = consumer.poll(1)
        if message is None:
            idle += 1
            continue
        if message.error():
            if message.error().code() == KafkaError._PARTITION_EOF:
                ended.add(message.partition())
            continue
        state[message.key()] = message.value()
    consumer.close()
    return {k: v for k, v in state.items() if v is not None}


def rewrite(b):
    """The orders rewritten many times (8 KiB values) and two removed."""
    producer = Producer({**b["as_user"]("app"), "enable.idempotence": True})
    for version in range(24):
        for i in range(1, 31):
            producer.produce(b["names"]["src"], key=f"order-{i}".encode(),
                             value=(f'{{"order": {i}, "v": {version}}}'.ljust(8192)).encode(),
                             partition=i % 3)
        producer.flush(15)
    for i in (5, 6):
        producer.produce(b["names"]["src"], key=f"order-{i}".encode(), value=None,
                         partition=i % 3)
    producer.flush(15)


@pytest.fixture
def compacted(kafka_compacted_business):
    """Rewritten, then waited for until the source has compacted."""
    b = kafka_compacted_business
    rewrite(b)
    deadline = time.monotonic() + 180
    while not compacted_away(b, b["names"]["src"]):
        assert time.monotonic() < deadline, "the source did not compact"
        time.sleep(5)
    return b


def test_a_compacted_topic_is_handed_off_by_its_meaning(compacted):
    b = compacted
    world = KafkaWorld(b)
    assert compacted_away(b, b["names"]["src"]) > 0  # records really were compacted
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(world.adapter(), action)
        assert readback(world.adapter(), action) == EffectStatus.APPLIED, action
    apply(world.adapter(), "copy_content")  # again: the target is recreated, not emptied
    for action in ("copy_content", "verify_final_state"):
        assert readback(world.adapter(), action) == EffectStatus.APPLIED, action
    state = latest(b, b["names"]["src"])
    assert len(state) == 28 and latest(b, b["names"]["tgt"]) == state  # 2 orders removed
    by_principal = {p: key for key, p in b["principals"].items()}
    probes = KafkaTopicProbes(
        config=lambda side: dict(b["settings"]),
        config_as=lambda side, principal: b["as_user"](by_principal[principal]),
        topics={"source": b["names"]["src"], "target": b["names"]["tgt"]},
        producers=(b["principals"]["app"], b["principals"]["batch"]),
        app_producer=b["principals"]["app"], probe_prefix=b["probe"], groups=(b["group"],),
        sets={"orders": b["names"]["src"]})
    for check in (lambda: probes.content_equal("orders"), lambda: probes.restore_drill()):
        passed, observation = check()
        assert passed, observation
    apply(world.adapter(), "activate_target")
    assert readback(world.adapter(), "activate_target") == EffectStatus.APPLIED


def test_a_sync_then_the_final_step_carry_what_compaction_left(compacted):
    b = compacted
    world = KafkaWorld(b)
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    producer = Producer({**b["as_user"]("app"), "enable.idempotence": True})
    producer.produce(b["names"]["src"], key=b"order-7", value=b'{"order": 7, "v": "late"}',
                     partition=1)
    producer.produce(b["names"]["src"], key=b"order-8", value=None, partition=2)
    producer.flush(15)
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    state = latest(b, b["names"]["tgt"])
    assert state == latest(b, b["names"]["src"]) and state[b"order-7"].endswith(b'"late"}')
    assert b"order-8" not in state


def test_a_changed_value_is_still_a_difference(compacted):
    b = compacted
    world = KafkaWorld(b)
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(world.adapter(), action)
    producer = Producer(b["settings"])
    producer.produce(b["names"]["tgt"], key=b"order-9", value=b'{"forged": true}', partition=0)
    producer.flush(15)
    assert readback(world.adapter(), "copy_content") == EffectStatus.UNKNOWN


def test_compaction_after_the_copy_does_not_change_the_verdict(kafka_compacted_business):
    """Copied before the source compacts: the record sequences then part, the
    meaning does not."""
    b = kafka_compacted_business
    world = KafkaWorld(b)
    apply(world.adapter(), "stage_target")
    # The source compacts its closed segments on its own schedule. If that ran
    # before the copy, nothing is left to compact after it: write and copy
    # again (the earlier active segment then closes, dirty) until it does.
    for attempt in range(3):
        rewrite(b)
        for action in ("pause_admission", "fence_source_writes", "copy_content"):
            apply(world.adapter(), action)
        copied = compacted_away(b, b["names"]["src"])
        deadline = time.monotonic() + 90
        while compacted_away(b, b["names"]["src"]) <= copied and time.monotonic() < deadline:
            time.sleep(5)  # the source compacts on its own
        if compacted_away(b, b["names"]["src"]) > copied:
            break
        world.reopen_source()  # the writers again, for the next rewrite
    else:
        raise AssertionError("the source did not compact after any of three copies")
    assert readback(world.adapter(), "copy_content") == EffectStatus.APPLIED
