"""A Kafka-API topic handed off between two separate clusters by the whole
control plane: the source on the shared broker of one kind cluster (SASL,
plain), the target a broker of its own in another (SASL over TLS). Offsets
differ between the clusters; each consumer group resumes on the target at
the record it would have read next on the source."""

import json

from confluent_kafka import Consumer
from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import without_transport_control
from kafka_world import Route
import pytest

from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.service.kafka_execution import kafka_factory, kafka_resolver

pytestmark = [pytest.mark.postgres, pytest.mark.kafka, pytest.mark.kubernetes]
EVENTS = {"id": "ORDER-02", "description": "모든 주문 이벤트는 주문 번호를 가진 JSON이다."}


def _file(tmp_path, name, found):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(found))
    return str(path)


def test_a_topic_moves_to_another_cluster(postgres_database, now, kafka_cross_business,
                                          tmp_path):
    b, t = kafka_cross_business, kafka_cross_business["target"]
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + b["names"]["src"][7:15] * 4
    data["spec"]["data"]["protected_sets"] = ["orders"]
    data["spec"]["business"]["invariants"] = [EVENTS]
    without_transport_control(data)  # the source cluster speaks SASL without TLS
    h = prepared(postgres_database, now, tmp_path, data, engine="kafka")
    clusters = {"source": _file(tmp_path, "source", b["settings"]),
                "target": _file(tmp_path, "target", t["settings"])}
    own = {p: {side: _file(tmp_path, f"{key}-{side}", b["as_user_on"](key, side))
               for side in ("source", "target")} for key, p in b["principals"].items()}
    entry = {"clusters": clusters,
             "topics": {"source": b["names"]["src"], "target": b["names"]["tgt"]},
             "producers": [b["principals"]["app"], b["principals"]["batch"]],
             "app_producer": b["principals"]["app"], "consumers": [b["principals"]["reader"]],
             "groups": [b["group"]], "transactional_prefixes": [b["prefix"]],
             "probe_prefix": b["probe"], "principal_settings": own,
             "sets": {"orders": b["names"]["src"]},
             "invariants": {"ORDER-02": {"type": "json_field", "field": "order"}},
             "other_topic": b["other_topic"],
             "zone_of": {"source": "approved-zone-a", "target": "approved-zone-a"}}
    route = Route(b["names"]["src"])
    resolve, _ = kafka_resolver({"tenant_id": h["tenant"], "application_id": h["app"],
                                 "kafka": entry, "route": {}}, route_for=lambda _: route)
    worker = worker_for(h, kafka_factory(h["adapter_id"], resolve),
                        owner="worker-kafka-across")
    record = worker.drive(h["tenant"], h["app"], h["unit"])
    results = signed_results(h)
    assert record.state == HandoffState.HANDOFF_COMPLETE, (
        record.reason, {k: h["observations"].get(k) for k, v in results.items() if v != "pass"})
    assert route.get() == b["names"]["tgt"] and set(results.values()) == {"pass"}
    # The reader resumes on the other cluster: the 18 records it had not read.
    consumer = Consumer({**b["as_user_on"]("reader", "target"), "group.id": b["group"],
                         "enable.auto.commit": False, "isolation.level": "read_committed",
                         "auto.offset.reset": "earliest"})
    consumer.subscribe([b["names"]["tgt"]])
    got, idle = [], 0
    while idle < 8:
        message = consumer.poll(1)
        if message is None or message.error():
            idle += 1
            continue
        got.append(message.key())
    consumer.close()
    assert len(got) == 18, len(got)
    print("measured:", json.dumps({k: v for k, v in h["observations"].items()
                                   if k.startswith("availability.")}))
