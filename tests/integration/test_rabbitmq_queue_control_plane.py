"""A RabbitMQ queue (the order work queue) handed off by the whole control
plane, and the queue probes failing where they must. Messages are never read
by the probes: what they measure is who may publish, definitions and counts."""

import json
from urllib.parse import quote

from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import without_transport_control
import pytest
from rabbitmq_business import EXCHANGE, PASSWORD, QUEUE, ROUTING_KEY
from rabbitmq_world import Route

from sova.adapters.rabbitmq_queue_probes import RabbitMqQueueProbes
from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.service.rabbitmq_execution import rabbitmq_factory

pytestmark = [pytest.mark.postgres, pytest.mark.rabbitmq]
QUEUE_01 = {"id": "QUEUE-01", "description": "인계로 처리 대기 작업이 사라지거나 늘지 않는다."}


@pytest.fixture
def other_vhost(rabbitmq_business):
    """Another tenant's virtual host on the same broker."""
    name = rabbitmq_business["vhosts"]["src"].replace("orders-", "shop-")
    rabbitmq_business["api"]("PUT", f"/vhosts/{quote(name, safe='')}", {})
    yield name
    rabbitmq_business["api"]("DELETE", f"/vhosts/{quote(name, safe='')}")


def settings_of(b, other):
    import pika

    def broker(side):
        return {"http": b["http"], "auth": b["admin"], "amqp": lambda vhost: b["params"](vhost)}

    def connect_as(side, user, vhost):
        return pika.ConnectionParameters(b["host"], b["port"], vhost,
                                         pika.PlainCredentials(user, PASSWORD),
                                         connection_attempts=1, socket_timeout=5)

    return {"broker": broker, "connect_as": connect_as,
            "vhosts": {"source": b["vhosts"]["src"], "target": b["vhosts"]["tgt"]},
            "queue": QUEUE, "exchange": EXCHANGE, "routing_key": ROUTING_KEY,
            "publishers": (b["users"]["app"], b["users"]["batch"]),
            "app_publisher": b["users"]["app"], "consumers": (b["users"]["reader"],),
            "admin_user": b["admin"][0], "other_vhost": other,
            "invariants": {"QUEUE-01": {"type": "same_count"}},
            "zone_of": lambda side: "approved-zone-a"}


def test_a_queue_is_handed_off_by_the_control_plane(postgres_database, now, rabbitmq_business,
                                                    other_vhost, tmp_path):
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + rabbitmq_business["vhosts"]["src"][7:15] * 4
    data["spec"]["data"]["protected_sets"] = ["order-work"]
    data["spec"]["business"]["invariants"] = [QUEUE_01]
    without_transport_control(data)  # the disposable broker speaks AMQP without TLS
    h = prepared(postgres_database, now, tmp_path, data, engine="rabbitmq")
    route = Route(rabbitmq_business["vhosts"]["src"])
    settings = {**settings_of(rabbitmq_business, other_vhost), "route": route}
    worker = worker_for(h, rabbitmq_factory(h["adapter_id"], lambda t, a: settings),
                        owner="worker-rabbitmq")
    record = worker.drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert route.get() == rabbitmq_business["vhosts"]["tgt"]
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    seen = h["observations"]
    assert seen["data.order-work"]["source"]["ready"] == 22  # 30 published, 8 handled
    assert seen["operations.restore_test"]["restored"] == "definitions"
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith("availability.")}))


def test_each_queue_probe_fails_on_what_it_measures(rabbitmq_business, other_vhost):
    from adapter_conformance import apply
    from rabbitmq_world import RabbitMqWorld

    b = rabbitmq_business
    world = RabbitMqWorld(b)
    p = RabbitMqQueueProbes(**{k: v for k, v in settings_of(b, other_vhost).items()
                               if k not in ("consumers", "admin_user")})
    passed, live = p.write_denied("source")
    assert not passed and {o["attempt"] for o in live["outcomes"].values()} == {
        "write_accepted"}
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
                   "activate_target"):
        apply(world.adapter(), action)
    for check in (lambda: p.write_denied("source"), p.single_writer,
                  lambda: p.content_equal("order-work"), lambda: p.invariant("QUEUE-01"),
                  p.cross_tenant_denied,
                  lambda: p.restore_drill("sova-restore")):
        passed, observation = check()
        assert passed, observation
    world.tamper_target()  # one more message on the target
    assert not p.content_equal("order-work")[0]
    assert not p.invariant("QUEUE-01")[0]  # the forged message changed the count
    assert not p.invariant("ORDER-01")[0]  # nothing measured over unread messages
    assert not p.transport_encrypted("target")[0]
    # A publisher given the source's exchange again is not denied, whatever its login.
    world.reopen_source()
    assert not p.write_denied("source")[0]
