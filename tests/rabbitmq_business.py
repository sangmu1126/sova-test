"""A real RabbitMQ queue for handoff tests: a durable quorum queue fed by a
topic exchange, an application publisher, a second publishing path (a batch
job) and a consumer, with messages already published and partly consumed
before any handoff starts. Source and target are two virtual hosts of the one
loopback broker.

``SOVA_TEST_RABBITMQ`` (AMQP ``host:port``), ``SOVA_TEST_RABBITMQ_HTTP`` (the
management API) and ``SOVA_TEST_RABBITMQ_ADMIN`` (``user:password`` of an
administrator) name it. Each test gets its own virtual hosts and users.
"""

import os
from urllib.parse import quote, urlsplit
from uuid import uuid4

import pytest

PASSWORD = "sova-user-test-secret"
QUEUE, EXCHANGE, ROUTING_KEY = "orders-work", "orders", "order.created"


def broker():
    address, http, admin = (os.environ.get("SOVA_TEST_RABBITMQ"),
                            os.environ.get("SOVA_TEST_RABBITMQ_HTTP"),
                            os.environ.get("SOVA_TEST_RABBITMQ_ADMIN"))
    if not address or not http or not admin:
        pytest.skip("SOVA_TEST_RABBITMQ, SOVA_TEST_RABBITMQ_HTTP and SOVA_TEST_RABBITMQ_ADMIN "
                    "are required for RabbitMQ tests")
    pytest.importorskip("pika")
    host, port = urlsplit("//" + address).hostname, urlsplit("//" + address).port
    if host not in ("127.0.0.1", "localhost") or urlsplit(http).hostname not in (
            "127.0.0.1", "localhost"):
        raise pytest.UsageError("RabbitMQ tests run only against a loopback broker")
    return host, port, http, tuple(admin.split(":", 1))


@pytest.fixture
def rabbitmq_business():
    import httpx
    import pika

    host, port, http, admin = broker()
    suffix = uuid4().hex[:8]
    vhosts = {"src": f"orders-{suffix}-src", "tgt": f"orders-{suffix}-tgt"}
    users = {k: f"orders-{k}-{suffix}" for k in ("app", "batch", "reader")}

    def api(method, path, body=None):
        r = httpx.request(method, http.rstrip("/") + "/api" + path, json=body, auth=admin,
                          timeout=10)
        if r.status_code not in (404,) or method != "DELETE":
            r.raise_for_status()
        return r.json() if r.content else None

    def params(vhost, user=None):
        credentials = pika.PlainCredentials(*(admin if user is None else (user, PASSWORD)))
        return pika.ConnectionParameters(host, port, vhost, credentials,
                                         connection_attempts=1, socket_timeout=5,
                                         blocked_connection_timeout=10)

    src = quote(vhosts["src"], safe="")
    api("PUT", f"/vhosts/{src}", {})
    api("PUT", f"/permissions/{src}/{admin[0]}", {"configure": ".*", "write": ".*",
                                                  "read": ".*"})
    for user in users.values():
        api("PUT", f"/users/{user}", {"password": PASSWORD, "tags": ""})
    api("PUT", f"/exchanges/{src}/{EXCHANGE}", {"type": "topic", "durable": True})
    api("PUT", f"/queues/{src}/{QUEUE}", {"durable": True,
                                          "arguments": {"x-queue-type": "quorum"}})
    api("POST", f"/bindings/{src}/e/{EXCHANGE}/q/{QUEUE}", {"routing_key": "order.#"})
    grants = {"app": ("^$", f"^{EXCHANGE}$", "^$"), "batch": ("^$", f"^{EXCHANGE}$", "^$"),
              "reader": ("^$", "^$", f"^{QUEUE}$")}
    for key, (configure, write, read) in grants.items():
        api("PUT", f"/permissions/{src}/{users[key]}",
            {"configure": configure, "write": write, "read": read})
    connection = pika.BlockingConnection(params(vhosts["src"], users["app"]))
    channel = connection.channel()
    channel.confirm_delivery()
    for i in range(1, 31):  # business before the handoff
        channel.basic_publish(EXCHANGE, ROUTING_KEY, f'{{"order": {i}}}'.encode(),
                              pika.BasicProperties(content_type="application/json",
                                                   delivery_mode=2, message_id=f"order-{i}",
                                                   headers={"source": "orders-api"}))
    connection.close()
    connection = pika.BlockingConnection(params(vhosts["src"], users["reader"]))
    channel = connection.channel()
    for _ in range(8):  # the consumer has handled part of the queue
        method, _, _ = channel.basic_get(QUEUE, auto_ack=False)
        channel.basic_ack(method.delivery_tag)
    connection.close()
    yield {"vhosts": vhosts, "users": users, "admin": admin, "api": api, "params": params,
           "http": http, "grants": grants, "host": host, "port": port}
    for vhost in vhosts.values():
        api("DELETE", f"/vhosts/{quote(vhost, safe='')}")
    for user in users.values():
        api("DELETE", f"/users/{user}")
