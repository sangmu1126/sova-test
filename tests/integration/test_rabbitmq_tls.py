"""A RabbitMQ queue over verified TLS through the worker's own execution
settings: a broker that speaks only TLS (AMQP and the management API), the
settings file naming the CA, and what the resolver builds from it carrying a
whole control-plane handoff with the transport obligations measured, not
waived. Both channels carry the administrator's credentials, so both must be
verified; a management API over plain http next to a CA is refused."""

import json
from urllib.parse import quote

from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import local_budgets
from mysql_pair_business import tls_material
import pytest
from rabbitmq_business import EXCHANGE, PASSWORD, QUEUE, ROUTING_KEY
from rabbitmq_world import Route

from sova.adapters.rabbitmq_queue_probes import RabbitMqQueueProbes
from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError
from sova.domain.handoff_authority import HandoffState
from sova.service.rabbitmq_execution import rabbitmq_factory, rabbitmq_resolver

pytestmark = [pytest.mark.postgres, pytest.mark.rabbitmq, pytest.mark.kubernetes]
QUEUE_01 = {"id": "QUEUE-01", "description": "인계로 처리 대기 작업이 사라지거나 늘지 않는다."}


def _entry(tmp_path, b, other, *, ca=None, http=None, name="tls"):
    brokers = {}
    for side in ("source", "target"):
        path = tmp_path / f"broker-{name}-{side}.json"
        path.write_text(json.dumps({
            "http": http or b["http"], "host": b["host"], "port": b["port"],
            "username": b["admin"][0], "password": b["admin"][1],
            **({"ssl_ca": ca} if ca else {})}))
        brokers[side] = str(path)
    passwords = {}
    for user in b["users"].values():
        path = tmp_path / f"{user}.password"
        path.write_text(PASSWORD)
        passwords[user] = str(path)
    return {"brokers": brokers,
            "vhosts": {"source": b["vhosts"]["src"], "target": b["vhosts"]["tgt"]},
            "queue": QUEUE, "exchange": EXCHANGE, "routing_key": ROUTING_KEY,
            "publishers": [b["users"]["app"], b["users"]["batch"]],
            "app_publisher": b["users"]["app"], "consumers": [b["users"]["reader"]],
            "user_password_files": passwords,
            "invariants": {"QUEUE-01": {"type": "same_count"}}, "other_vhost": other,
            "zone_of": {"source": "approved-zone-a", "target": "approved-zone-a"}}


def _resolve(entry, tenant, application, route):
    app = {"tenant_id": tenant, "application_id": application, "rabbitmq": entry,
           "route": {"kind": "test"}}
    resolve, _ = rabbitmq_resolver(app, route_for=lambda _: route)
    return resolve(tenant, application)


def _probes(d):
    return RabbitMqQueueProbes(
        broker=d["broker"], connect_as=d["connect_as"], vhosts=d["vhosts"], queue=d["queue"],
        exchange=d["exchange"], routing_key=d["routing_key"], publishers=d["publishers"],
        app_publisher=d["app_publisher"], other_vhost=d["other_vhost"])


@pytest.fixture
def other_vhost(rabbitmq_tls_business):
    name = rabbitmq_tls_business["vhosts"]["src"].replace("orders-", "shop-")
    rabbitmq_tls_business["api"]("PUT", f"/vhosts/{quote(name, safe='')}", {})
    yield name
    rabbitmq_tls_business["api"]("DELETE", f"/vhosts/{quote(name, safe='')}")


def test_a_queue_is_handed_off_over_verified_tls(postgres_database, now, rabbitmq_tls_business,
                                                 other_vhost, tmp_path):
    b = rabbitmq_tls_business
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + b["vhosts"]["src"][7:15] * 4
    data["spec"]["data"]["protected_sets"] = ["order-work"]
    data["spec"]["business"]["invariants"] = [QUEUE_01]
    local_budgets(data)  # transport control kept
    h = prepared(postgres_database, now, tmp_path, data, engine="rabbitmq")
    route = Route(b["vhosts"]["src"])
    resolved = _resolve(_entry(tmp_path, b, other_vhost, ca=b["ca_file"]), h["tenant"],
                        h["app"], route)
    worker = worker_for(h, rabbitmq_factory(h["adapter_id"], lambda t, a: resolved),
                        owner="worker-rabbitmq-tls")
    record = worker.drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert route.get() == b["vhosts"]["tgt"]
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    seen = h["observations"]
    transport = [o for o in seen.values() if o.get("probe") == "transport_encrypted"]
    assert transport and all(o["client_verification"] == "verify-identity"
                             and o["management"] == "https" for o in transport), transport
    assert seen["data.order-work"]["target"]["ready"] == 22  # 30 published, 8 handled


def test_unverified_or_plain_channels_do_not_pass(rabbitmq_tls_business, other_vhost, tmp_path):
    import ssl

    import httpx
    import pika

    b = rabbitmq_tls_business
    args = ("tenant-x", "app-x", Route(b["vhosts"]["src"]))
    with pytest.raises(ContractError, match="https"):  # a CA next to a plain management API
        _resolve(_entry(tmp_path, b, other_vhost, ca=b["ca_file"],
                        http=b["http"].replace("https://", "http://"), name="mixed"), *args)
    plain = _resolve(_entry(tmp_path, b, other_vhost, name="plain"), *args)
    passed, observation = _probes(plain).transport_encrypted("target")
    assert not passed and observation["tls"] is False, observation
    other_ca = tmp_path / "other-ca.pem"  # a CA that does not vouch for the broker
    other_ca.write_bytes(tls_material("elsewhere")["ca.pem"])
    wrong = _resolve(_entry(tmp_path, b, other_vhost, ca=str(other_ca), name="wrong"), *args)
    with pytest.raises(ssl.SSLCertVerificationError):  # pika raises the handshake's own
        pika.BlockingConnection(wrong["broker"]("target")["amqp"](b["vhosts"]["src"]))
    # A write attempt that cannot verify the broker decides nothing (counts against).
    wrote, outcome = _probes(wrong)._try_publish("source", b["users"]["app"],
                                                 b["vhosts"]["src"])
    assert not wrote and outcome == "undecided:SSLCertVerificationError", outcome
    with pytest.raises(httpx.ConnectError):
        broker = wrong["broker"]("target")
        httpx.get(broker["http"] + "/api/overview", auth=broker["auth"],
                  verify=broker["verify"], timeout=5)
