"""The RabbitMQ queue adapter against the provider-neutral conformance kit,
and what a queue adds: messages that cannot be read without changing them,
and permission checks a channel caches."""

from adapter_conformance import HandoffAdapterConformance, apply, readback
import pytest
from rabbitmq_business import EXCHANGE, QUEUE, ROUTING_KEY
from rabbitmq_world import RabbitMqWorld

from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.rabbitmq


@pytest.fixture
def world(rabbitmq_business):
    return RabbitMqWorld(rabbitmq_business)


class TestRabbitMqQueueConformance(HandoffAdapterConformance):
    pass


HANDOFF = ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
           "verify_final_state", "activate_target", "commit_route")


def test_the_target_delivers_the_same_messages_in_order_as_first_deliveries(world):
    for action in HANDOFF:
        apply(world.adapter(), action)
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    connection = world._amqp(world.vhosts["tgt"], world.users["reader"])
    channel = connection.channel()
    delivered = []
    while True:
        method, properties, body = channel.basic_get(QUEUE, auto_ack=True)
        if method is None:
            break
        delivered.append((properties.message_id, body, method.redelivered,
                          properties.content_type, (properties.headers or {}).get("source")))
    connection.close()
    # 30 published, 8 handled before the handoff: the other 22, in their order.
    assert [d[0] for d in delivered] == [f"order-{i}" for i in range(9, 31)]
    assert all(not redelivered for _, _, redelivered, _, _ in delivered)
    assert {(d[3], d[4]) for d in delivered} == {("application/json", "orders-api")}
    assert world.content("source") == 22  # the source keeps its copy, unread


def test_a_channel_opened_before_the_fence_cannot_publish_after_it(world):
    """Permission checks are cached per channel: revoking alone would leave
    this publisher writing. The fence ends its connection - here a client that
    does not even answer the close, so the broker drops it at its closing
    timeout, and the fence waits for that."""
    import time

    import pika

    connection = world._amqp(world.vhosts["src"], world.users["app"])
    channel = connection.channel()
    channel.confirm_delivery()
    channel.basic_publish(EXCHANGE, ROUTING_KEY, b'{"before": true}')
    apply(world.adapter(), "stage_target")
    apply(world.adapter(), "pause_admission")
    started = time.monotonic()
    apply(world.adapter(), "fence_source_writes")
    print(f"\nfence with an unresponsive client: {time.monotonic() - started:.1f} s")
    assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED
    with pytest.raises((pika.exceptions.AMQPError, pika.exceptions.StreamLostError)):
        channel.basic_publish(EXCHANGE, ROUTING_KEY, b'{"after": true}')
    assert world.content("source") == 23
