"""A Kafka-API topic over verified TLS through the worker's own execution
settings: a broker whose only listener is TLS (SASL/SCRAM inside it), the
settings files naming the CA, and what the resolver builds from them carrying
a whole control-plane handoff with the transport obligations measured, not
waived. Settings that skip verification, or a CA that does not vouch for the
broker, do not pass."""

import json

from confluent_kafka import KafkaException, Producer
from confluent_kafka.admin import AdminClient, NewTopic
from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import local_budgets
from kafka_business import settings
from kafka_world import Route
from mysql_pair_business import tls_material
import pytest
from tls_checks import endpoint, identity_held, summary

from sova.adapters.kafka_topic_probes import KafkaTopicProbes
from sova.adapters.tls_refusal import KAFKA_API_VERSIONS, hello_plaintext, transport_refusals
from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.service.kafka_execution import kafka_factory, kafka_resolver

pytestmark = [pytest.mark.postgres, pytest.mark.kafka, pytest.mark.kubernetes]
EVENTS = {"id": "ORDER-02", "description": "모든 주문 이벤트는 주문 번호를 가진 JSON이다."}


@pytest.fixture
def other_topic(kafka_tls_business):
    name = kafka_tls_business["names"]["src"].replace("orders-", "shop-")
    admin = kafka_tls_business["admin"]
    admin.create_topics([NewTopic(name, 1, 1)])[name].result()
    producer = Producer(kafka_tls_business["settings"])
    producer.produce(name, value=b'{"shop": true}', partition=0)
    producer.flush(15)
    yield name
    admin.delete_topics([name])


def _file(tmp_path, name, found):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(found))
    return str(path)


def _entry(tmp_path, b, other, *, ca, extra=None, name="tls"):
    from kafka_business import PASSWORD

    admin = _file(tmp_path, f"{name}-admin", {**settings(b["address"], *b["root"], ca),
                                             **(extra or {})})
    own = {p: _file(tmp_path, f"{name}-{key}", settings(b["address"], b["users"][key],
                                                         PASSWORD, ca))
           for key, p in b["principals"].items()}
    return {"clusters": {"source": admin, "target": admin},
            "topics": {"source": b["names"]["src"], "target": b["names"]["tgt"]},
            "producers": [b["principals"]["app"], b["principals"]["batch"]],
            "app_producer": b["principals"]["app"], "consumers": [b["principals"]["reader"]],
            "groups": [b["group"]], "transactional_prefixes": [b["prefix"]],
            "probe_prefix": b["probe"],
            "principal_settings": {p: {"source": f, "target": f} for p, f in own.items()},
            "sets": {"orders": b["names"]["src"]},
            "invariants": {"ORDER-02": {"type": "json_field", "field": "order"}},
            "other_topic": other,
            "zone_of": {"source": "approved-zone-a", "target": "approved-zone-a"}}


def _resolve(entry, tenant, application, route):
    app = {"tenant_id": tenant, "application_id": application, "kafka": entry,
           "route": {"kind": "test"}}
    resolve, _ = kafka_resolver(app, route_for=lambda _: route)
    return resolve(tenant, application)


def _probes(d):
    return KafkaTopicProbes(config=d["config"], config_as=d["config_as"], topics=d["topics"],
                            producers=d["producers"], app_producer=d["app_producer"],
                            probe_prefix=d["probe_prefix"], timeout=10.0)


def test_a_topic_is_handed_off_over_verified_tls(postgres_database, now, kafka_tls_business,
                                                 other_topic, tmp_path):
    b = kafka_tls_business
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + b["names"]["src"][7:15] * 4
    data["spec"]["data"]["protected_sets"] = ["orders"]
    data["spec"]["business"]["invariants"] = [EVENTS]
    local_budgets(data)  # transport control kept
    h = prepared(postgres_database, now, tmp_path, data, engine="kafka")
    route = Route(b["names"]["src"])
    resolved = _resolve(_entry(tmp_path, b, other_topic, ca=b["ca_file"]), h["tenant"],
                        h["app"], route)
    worker = worker_for(h, kafka_factory(h["adapter_id"], lambda t, a: resolved),
                        owner="worker-kafka-tls")
    record = worker.drive(h["tenant"], h["app"], h["unit"])
    results = signed_results(h)
    assert record.state == HandoffState.HANDOFF_COMPLETE, (
        record.reason, {k: h["observations"].get(k) for k, v in results.items() if v != "pass"})
    assert route.get() == b["names"]["tgt"]
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    transport = [o for o in h["observations"].values()
                 if o.get("probe") == "transport_encrypted"]
    assert transport and all(o["client_verification"] == "verify-identity" and o["connected"]
                             for o in transport), transport


def test_unverified_or_plain_settings_do_not_pass(kafka_tls_business, other_topic, tmp_path):
    b = kafka_tls_business
    args = ("tenant-x", "app-x", Route(b["names"]["src"]))
    plain = _resolve(_entry(tmp_path, b, other_topic, ca=None, name="plain"), *args)
    passed, observation = _probes(plain).transport_encrypted("target")
    assert not passed and observation["tls"] is False, observation
    unchecked = _resolve(_entry(tmp_path, b, other_topic, ca=b["ca_file"], name="unchecked",
                                extra={"ssl.endpoint.identification.algorithm": "none"}), *args)
    passed, observation = _probes(unchecked).transport_encrypted("target")
    assert not passed and observation["client_verification"] == "none", observation
    other_ca = tmp_path / "other-ca.pem"  # a CA that does not vouch for the broker
    other_ca.write_bytes(tls_material("elsewhere")["ca.pem"])
    wrong = _resolve(_entry(tmp_path, b, other_topic, ca=str(other_ca), name="wrong"), *args)
    passed, observation = _probes(wrong).transport_encrypted("target")
    assert not passed and observation["connected"] is False, observation
    with pytest.raises(KafkaException):
        AdminClient(wrong["config"]("target")).list_topics(timeout=5)


def test_a_kafka_api_listening_only_over_tls_refuses_plaintext(kafka_tls_broker):
    b = kafka_tls_broker
    port = int(b["address"].rsplit(":", 1)[1])
    passed, observation = transport_refusals(endpoint(port, b["ca_file"]),
                                             plaintext=hello_plaintext(KAFKA_API_VERSIONS))
    assert passed and identity_held(observation), summary(observation)
