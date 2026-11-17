"""A RabbitMQ queue handed off between two separate brokers by the whole
control plane: the source on the shared broker of one kind cluster (plain
AMQP), the target a broker of its own in another (AMQP and the management
API over TLS only). Waiting work arrives once, definitions included."""

import json

from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import without_transport_control
import pika
import pytest
from rabbitmq_business import EXCHANGE, PASSWORD, QUEUE, ROUTING_KEY
from rabbitmq_world import Route

from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.service.rabbitmq_execution import rabbitmq_factory, rabbitmq_resolver

pytestmark = [pytest.mark.postgres, pytest.mark.rabbitmq, pytest.mark.kubernetes]
QUEUE_01 = {"id": "QUEUE-01", "description": "인계로 처리 대기 작업이 사라지거나 늘지 않는다."}


def _file(tmp_path, name, found):
    path = tmp_path / name
    path.write_text(found if isinstance(found, str) else json.dumps(found))
    return str(path)


def test_a_queue_moves_to_another_broker(postgres_database, now, rabbitmq_cross_business,
                                         tmp_path):
    b, t = rabbitmq_cross_business, rabbitmq_cross_business["target"]
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + b["vhosts"]["src"][7:15] * 4
    data["spec"]["data"]["protected_sets"] = ["order-work"]
    data["spec"]["business"]["invariants"] = [QUEUE_01]
    without_transport_control(data)  # the source broker speaks AMQP without TLS
    h = prepared(postgres_database, now, tmp_path, data, engine="rabbitmq")
    brokers = {
        "source": _file(tmp_path, "source.json", {
            "http": b["http"], "host": b["host"], "port": b["port"],
            "username": b["admin"][0], "password": b["admin"][1]}),
        "target": _file(tmp_path, "target.json", {
            "http": t["http"], "host": t["host"], "port": t["port"],
            "username": t["admin"][0], "password": t["admin"][1], "ssl_ca": t["ca_file"]})}
    entry = {"brokers": brokers,
             "vhosts": {"source": b["vhosts"]["src"], "target": b["vhosts"]["tgt"]},
             "queue": QUEUE, "exchange": EXCHANGE, "routing_key": ROUTING_KEY,
             "publishers": [b["users"]["app"], b["users"]["batch"]],
             "app_publisher": b["users"]["app"], "consumers": [b["users"]["reader"]],
             "user_password_files": {u: _file(tmp_path, f"{u}.password", PASSWORD)
                                     for u in b["users"].values()},
             "invariants": {"QUEUE-01": {"type": "same_count"}},
             "other_vhost": b["other_vhost"],
             "zone_of": {"source": "approved-zone-a", "target": "approved-zone-a"}}
    route = Route(b["vhosts"]["src"])
    resolve, _ = rabbitmq_resolver({"tenant_id": h["tenant"], "application_id": h["app"],
                                    "rabbitmq": entry, "route": {}}, route_for=lambda _: route)
    worker = worker_for(h, rabbitmq_factory(h["adapter_id"], resolve),
                        owner="worker-rabbitmq-across")
    record = worker.drive(h["tenant"], h["app"], h["unit"])
    results = signed_results(h)
    assert record.state == HandoffState.HANDOFF_COMPLETE, (
        record.reason, {k: h["observations"].get(k) for k, v in results.items() if v != "pass"})
    assert route.get() == b["vhosts"]["tgt"] and set(results.values()) == {"pass"}
    # Counted over AMQP on the target (the management API's counts lag behind).
    target = pika.BlockingConnection(resolve(h["tenant"], h["app"])["broker"]("target")[
        "amqp"](b["vhosts"]["tgt"]))
    try:
        ready = target.channel().queue_declare(QUEUE, passive=True).method.message_count
    finally:
        target.close()
    assert ready == 22, ready  # 30 published, 8 handled on the source
    print("measured:", json.dumps({k: v for k, v in h["observations"].items()
                                   if k.startswith("availability.")}))
