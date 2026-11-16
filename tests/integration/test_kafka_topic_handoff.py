"""The Kafka-API topic adapter against the provider-neutral conformance kit
(Redpanda with SASL/SCRAM and ACLs), and what a stream adds: consumer
positions carried across clusters whose offsets differ."""

from adapter_conformance import HandoffAdapterConformance, apply, readback
from kafka_world import KafkaWorld
import pytest

from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.kafka


@pytest.fixture
def world(kafka_business):
    return KafkaWorld(kafka_business)


class TestKafkaTopicConformance(HandoffAdapterConformance):
    pass


def test_a_consumer_resumes_on_the_target_at_the_record_it_would_have_read_next(world):
    from confluent_kafka import Consumer

    b = world.b
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
                   "verify_final_state", "activate_target"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    seen = {}
    for topic in (world.names["src"], world.names["tgt"]):
        consumer = Consumer({**b["settings"], "group.id": b["group"],
                             "enable.auto.commit": False, "isolation.level": "read_committed",
                             "auto.offset.reset": "earliest"})
        consumer.subscribe([topic])
        got, idle = [], 0
        while idle < 8:
            message = consumer.poll(1)
            if message is None or message.error():
                idle += 1
                continue
            got.append((message.partition(), message.key(), message.value()))
        consumer.close()  # nothing committed: the source's position stays
        seen[topic] = sorted(got)
    # 30 records, 12 consumed before the handoff: the rest, once, on either side.
    assert len(seen[world.names["tgt"]]) == 18
    assert seen[world.names["src"]] == seen[world.names["tgt"]]


def test_copying_again_leaves_the_same_stream_not_a_longer_one(world):
    run = ("stage_target", "pause_admission", "fence_source_writes", "copy_content")
    for action in run:
        apply(world.adapter(), action)
    first = world.content("target")
    apply(world.adapter(), "copy_content")
    apply(world.adapter(), "apply_final_changes")
    assert world.content("target") == first == world.content("source")


def test_a_compact_only_topic_is_refused_before_anything_changes(world):
    from confluent_kafka.admin import AlterConfigOpType, ConfigEntry, ConfigResource
    from confluent_kafka.admin import ResourceType

    from sova.domain.contracts import ContractError

    resource = ConfigResource(ResourceType.TOPIC, world.names["src"], incremental_configs=[
        ConfigEntry("cleanup.policy", "compact",
                    incremental_operation=AlterConfigOpType.SET)])
    for future in world.b["admin"].incremental_alter_configs([resource]).values():
        future.result()
    before = world.state()
    with pytest.raises(ContractError):
        apply(world.adapter(), "stage_target")
    assert world.state() == before


def records_written(world):
    """Every offset the target topic has handed out (records and markers)."""
    from confluent_kafka import TopicPartition
    from confluent_kafka.admin import OffsetSpec

    found = world.b["admin"].list_offsets(
        {TopicPartition(world.names["tgt"], p): OffsetSpec.latest() for p in range(3)},
        request_timeout=15)
    return sum(f.result().offset for f in found.values())


def stop_window(world, steps):
    before = records_written(world)
    for action in ("pause_admission", "fence_source_writes", *steps, "verify_final_state"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    assert world.content("source") == world.content("target")
    return records_written(world) - before


def test_the_final_step_writes_only_what_came_since_the_sync(world):
    world.business_write(600)
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    world.business_write(5)  # the source still serves
    written = stop_window(world, ("apply_final_changes",))
    # The new records, and per partition at most two control batches: the commit
    # marker and the fence Redpanda writes when a transaction first touches it.
    assert written <= 5 + 2 * 3, written
    print(f"snapshot-cdc: {written} offsets written inside the stop")


def test_an_offline_copy_writes_the_whole_topic_inside_the_stop(world):
    world.business_write(600)
    apply(world.adapter(), "stage_target")
    written = stop_window(world, ("copy_content",))
    assert written >= 630, written
    print(f"offline copy: {written} offsets written inside the stop")


def test_a_target_that_is_not_the_sync_copy_gets_everything_again(world):
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    world.tamper_target()  # no longer a prefix of the source's records
    world.business_write(2)
    stop_window(world, ("apply_final_changes",))
