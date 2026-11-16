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


def business(host, port, http, admin, context=None):
    """The queue and its business on one broker; with ``context`` the broker is
    reached over verified TLS (AMQP and the management API)."""
    import httpx
    import pika

    suffix = uuid4().hex[:8]
    vhosts = {"src": f"orders-{suffix}-src", "tgt": f"orders-{suffix}-tgt"}
    users = {k: f"orders-{k}-{suffix}" for k in ("app", "batch", "reader")}

    def api(method, path, body=None):
        r = httpx.request(method, http.rstrip("/") + "/api" + path, json=body, auth=admin,
                          timeout=10, verify=context or True)
        if r.status_code not in (404,) or method != "DELETE":
            r.raise_for_status()
        return r.json() if r.content else None

    def params(vhost, user=None):
        credentials = pika.PlainCredentials(*(admin if user is None else (user, PASSWORD)))
        return pika.ConnectionParameters(host, port, vhost, credentials,
                                         ssl_options=pika.SSLOptions(context, host)
                                         if context else None,
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
           "http": http, "grants": grants, "host": host, "port": port, "context": context}
    for vhost in vhosts.values():
        api("DELETE", f"/vhosts/{quote(vhost, safe='')}")
    for user in users.values():
        api("DELETE", f"/users/{user}")


@pytest.fixture
def rabbitmq_business():
    yield from business(*broker())


RABBITMQ = ("docker.io/library/rabbitmq:4.1.8-management@sha256:"
            "479361ae8fbb8c852bf67f4acbbfa6f3f6810f8f08119ae6ad4d922bfeaa27c3")
TLS_ADMIN = ("sova-admin", "sova-rabbit-tls-test-secret")
TLS_CONFIG = f"""listeners.tcp = none
listeners.ssl.default = 5671
ssl_options.cacertfile = /etc/rabbitmq-tls/ca.pem
ssl_options.certfile = /etc/rabbitmq-tls/server-cert.pem
ssl_options.keyfile = /etc/rabbitmq-tls/server-key.pem
ssl_options.verify = verify_none
ssl_options.fail_if_no_peer_cert = false
management.ssl.port = 15671
management.ssl.cacertfile = /etc/rabbitmq-tls/ca.pem
management.ssl.certfile = /etc/rabbitmq-tls/server-cert.pem
management.ssl.keyfile = /etc/rabbitmq-tls/server-key.pem
default_user = {TLS_ADMIN[0]}
default_pass = {TLS_ADMIN[1]}
"""


def _tls_broker_objects():
    labels = {"app": "rabbitmq-tls"}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "rabbitmq-tls"},
         "data": {"90-tls.conf": TLS_CONFIG, "enabled_plugins": "[rabbitmq_management].\n"}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "rabbitmq"},
         "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {"containers": [{
                      "name": "rabbitmq", "image": RABBITMQ, "imagePullPolicy": "IfNotPresent",
                      "ports": [{"containerPort": 5671}, {"containerPort": 15671}],
                      "readinessProbe": {"tcpSocket": {"port": 15671}, "periodSeconds": 2},
                      "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                    "limits": {"memory": "512Mi"}},
                      "volumeMounts": [
                          {"name": "config", "mountPath": "/etc/rabbitmq/conf.d/90-tls.conf",
                           "subPath": "90-tls.conf"},
                          {"name": "config", "mountPath": "/etc/rabbitmq/enabled_plugins",
                           "subPath": "enabled_plugins"},
                          {"name": "tls", "mountPath": "/etc/rabbitmq-tls", "readOnly": True}]}],
                      "volumes": [{"name": "config", "configMap": {"name": "rabbitmq-tls"}},
                                  {"name": "tls", "secret": {"secretName": "rabbitmq-tls",
                                                             "defaultMode": 0o444}}]}}}}]


@pytest.fixture(scope="module")
def rabbitmq_tls_broker(tmp_path_factory):
    """A broker of the test module's own that speaks only TLS: AMQP on 5671 and
    the management API on https, under a test CA; reached by port-forward."""
    pytest.importorskip("pika")
    import base64
    import json
    import ssl
    import time

    import httpx

    from kind_support import delete_namespace, kind_namespace, kubectl
    from mysql_pair_business import tls_material
    from redis_business import _forward

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the RabbitMQ TLS tests")
    context, namespace, _ = kind_namespace([], _tls_broker_objects(), prefix="rabbitmq-tls")
    forwards = []
    try:
        material = tls_material(namespace)
        kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "rabbitmq-tls"},
            "data": {k: base64.b64encode(v).decode() for k, v in material.items()}}))
        ca_file = tmp_path_factory.mktemp("rabbitmq-tls") / "ca.pem"
        ca_file.write_bytes(material["ca.pem"])
        kubectl(context, "-n", namespace, "rollout", "status", "deployment/rabbitmq",
                "--timeout=300s")
        ports = {}
        for name, port in (("amqp", 5671), ("http", 15671)):
            process, ports[name] = _forward(context, namespace, "rabbitmq", port)
            forwards.append(process)
        tls = ssl.create_default_context(cafile=str(ca_file))
        http = f"https://127.0.0.1:{ports['http']}"
        deadline = time.monotonic() + 120
        while True:  # the management API answers once the broker has booted
            try:
                httpx.get(f"{http}/api/overview", auth=TLS_ADMIN, verify=tls,
                          timeout=5).raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield {"host": "127.0.0.1", "port": ports["amqp"], "http": http, "admin": TLS_ADMIN,
               "ca_file": str(ca_file), "context": tls}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


@pytest.fixture
def rabbitmq_tls_business(rabbitmq_tls_broker):
    b = rabbitmq_tls_broker
    for found in business(b["host"], b["port"], b["http"], b["admin"], b["context"]):
        yield {**found, "ca_file": b["ca_file"]}
