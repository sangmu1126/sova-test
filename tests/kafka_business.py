"""A real Kafka-API topic for handoff tests (Redpanda with SASL/SCRAM and ACLs):
an application producer with its transactional IDs, a second producer path
(a batch job), a consumer with its group, and records already produced and
partly consumed before any handoff starts.

``SOVA_TEST_KAFKA`` names the loopback broker, ``SOVA_TEST_KAFKA_ADMIN`` the
superuser ``user:password`` and ``SOVA_TEST_KAFKA_ADMIN_API`` the admin API
that manages users. Each test gets its own topics, principals and ACLs,
removed afterwards. Source and target are two topics on the one broker.
"""

import os
import time
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

PASSWORD = "sova-principal-test-secret"
PARTITIONS = 3


def broker():
    address, admin, api = (os.environ.get("SOVA_TEST_KAFKA"),
                           os.environ.get("SOVA_TEST_KAFKA_ADMIN"),
                           os.environ.get("SOVA_TEST_KAFKA_ADMIN_API"))
    if not address or not admin or not api:
        pytest.skip("SOVA_TEST_KAFKA, SOVA_TEST_KAFKA_ADMIN and SOVA_TEST_KAFKA_ADMIN_API "
                    "are required for Kafka tests")
    pytest.importorskip("confluent_kafka")
    if urlsplit("//" + address).hostname not in ("127.0.0.1", "localhost") or \
            urlsplit(api).hostname not in ("127.0.0.1", "localhost"):
        raise pytest.UsageError("Kafka tests run only against a loopback broker")
    user, password = admin.split(":", 1)
    return address, (user, password), api


def settings(address, user, password, ca=None):
    """Client settings for ``user``; with ``ca`` over TLS, the broker's
    certificate and host name verified."""
    found = {"bootstrap.servers": address, "security.protocol": "SASL_PLAINTEXT",
             "sasl.mechanism": "SCRAM-SHA-256", "sasl.username": user,
             "sasl.password": password}
    if ca:
        found |= {"security.protocol": "SASL_SSL", "ssl.ca.location": ca,
                  "ssl.endpoint.identification.algorithm": "https"}
    return found


def business(address, root, api, ca=None, config=None, archive=0):
    """The topic and its business on one broker (``ca``: reached over TLS;
    ``config``: the topic's own configuration; ``archive``: so many 8 KiB
    records produced three days ago, in partition 0 before the business)."""
    import httpx
    from confluent_kafka import Consumer, Producer, TopicPartition
    from confluent_kafka.admin import (
        AclBinding,
        AclBindingFilter,
        AclOperation,
        AclPermissionType,
        AdminClient,
        NewTopic,
        ResourcePatternType,
        ResourceType,
    )

    suffix = uuid4().hex[:8]
    names = {"src": f"orders-{suffix}-src", "tgt": f"orders-{suffix}-tgt"}
    users = {k: f"orders-{k}-{suffix}" for k in ("app", "batch", "reader")}
    principals = {k: f"User:{u}" for k, u in users.items()}
    group, prefix, probe = f"orders-consumers-{suffix}", f"orders-app-{suffix}-tx", \
        f"sova-probe-{suffix}-"
    admin_settings = settings(address, *root, ca)
    admin = AdminClient(admin_settings)
    for user in users.values():
        httpx.post(f"{api}/v1/security/users", json={
            "username": user, "password": PASSWORD, "algorithm": "SCRAM-SHA-256"},
            timeout=10).raise_for_status()
    created = admin.create_topics([NewTopic(names["src"], PARTITIONS, 1, config=config or {
        "cleanup.policy": "delete", "retention.ms": "604800000"})])
    created[names["src"]].result()

    def allow(resource, name, principal, operation, pattern=ResourcePatternType.LITERAL):
        return AclBinding(resource, name, pattern, principal, "*", operation,
                          AclPermissionType.ALLOW)

    T, G, X, Op = (ResourceType.TOPIC, ResourceType.GROUP, ResourceType.TRANSACTIONAL_ID,
                   AclOperation)
    grants = [allow(T, names["src"], principals["app"], Op.WRITE),
              allow(T, names["src"], principals["app"], Op.DESCRIBE),
              allow(X, prefix, principals["app"], Op.WRITE, ResourcePatternType.PREFIXED),
              allow(T, names["src"], principals["batch"], Op.WRITE),
              allow(T, names["src"], principals["reader"], Op.READ),
              allow(G, group, principals["reader"], Op.READ)]
    # Probe transactions (a write attempt that is always aborted) for each writer.
    grants += [allow(X, probe, principals[k], Op.WRITE, ResourcePatternType.PREFIXED)
               for k in ("app", "batch")]
    for future in admin.create_acls(grants).values():
        future.result()

    def as_user(key):
        return settings(address, users[key], PASSWORD, ca)

    producer = Producer({**as_user("app"), "enable.idempotence": True})
    then = int(time.time() * 1000) - 3 * 86_400_000
    for i in range(archive):  # old records: with small segments, closed ones of their own
        producer.produce(names["src"], key=f"archive-{i}".encode(), value=b"a" * 8192,
                         timestamp=then + i, partition=0)
    producer.flush(15)
    for i in range(1, 31):  # business before the handoff
        producer.produce(names["src"], key=f"order-{i}".encode(),
                         value=f'{{"order": {i}, "amount": {i * 10}}}'.encode(),
                         headers=[("source", b"orders-api")], partition=i % PARTITIONS)
    producer.flush(15)
    consumer = Consumer({**as_user("reader"), "group.id": group, "enable.auto.commit": False,
                         "auto.offset.reset": "earliest"})
    consumer.subscribe([names["src"]])
    read = 0
    while read < 12:  # the consumer has processed part of the stream
        message = consumer.poll(5)
        if message is not None and not message.error():
            read += 1
    consumer.commit(asynchronous=False)
    consumer.close()
    yield {"names": names, "principals": principals, "users": users, "group": group,
           "prefix": prefix, "probe": probe, "admin": admin, "settings": admin_settings,
           "as_user": as_user, "TopicPartition": TopicPartition, "address": address,
           "root": root, "ca": ca}
    admin.delete_topics([n for n in names.values()])
    for principal in principals.values():
        admin.delete_acls([AclBindingFilter(ResourceType.ANY, None, ResourcePatternType.ANY,
                                            principal, None, AclOperation.ANY,
                                            AclPermissionType.ANY)])
    admin.delete_consumer_groups([group])
    for user in users.values():
        httpx.delete(f"{api}/v1/security/users/{user}", timeout=10)


@pytest.fixture
def kafka_business():
    yield from business(*broker())


@pytest.fixture
def kafka_retained_business():
    """The business after 160 records three days old, on a topic rolling 1 MiB
    segments: the first holds old records only, so a shorter retention drops it."""
    yield from business(*broker(), config={
        "cleanup.policy": "delete", "retention.ms": "604800000", "segment.bytes": "1048576"},
        archive=160)


@pytest.fixture
def kafka_compacted_business():
    """The same business on a compact-only topic (a state topic: the latest value
    of each order), rolling small segments so compaction runs within a test."""
    yield from business(*broker(), config={
        "cleanup.policy": "compact", "segment.bytes": "1048576",
        "min.cleanable.dirty.ratio": "0.01", "delete.retention.ms": "1000"})


REDPANDA = ("docker.io/redpandadata/redpanda:v26.2.5@sha256:"
            "badca2d958f81da675c3055a9ecb6ebb590325582bcf16936148ffd65e96e920")
TLS_ROOT = ("sova-admin", "sova-kafka-tls-test-secret")


def _tls_broker_objects(port):
    """A Redpanda whose only Kafka listener is TLS, advertised as ``localhost:port``
    (the test's port-forward; ``localhost`` is in the server certificate)."""
    import json

    node = {"redpanda": {"data_directory": "/var/lib/redpanda/data",
                         "admin": [{"address": "0.0.0.0", "port": 9644}],  # the probes'
                         "kafka_api_tls": [{
        "name": "tls", "enabled": True, "cert_file": "/etc/redpanda-tls/server-cert.pem",
        "key_file": "/etc/redpanda-tls/server-key.pem",
        "truststore_file": "/etc/redpanda-tls/ca.pem", "require_client_auth": False}]}}
    bootstrap = {"enable_sasl": True, "superusers": [TLS_ROOT[0]],
                 "auto_create_topics_enabled": False}
    labels = {"app": "redpanda-tls"}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "redpanda-tls"},
         "data": {"redpanda.yaml": json.dumps(node), ".bootstrap.yaml": json.dumps(bootstrap)}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "redpanda"},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                  "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "initContainers": [{  # rpk rewrites its configuration: a copy
                          "name": "config", "image": REDPANDA, "imagePullPolicy": "IfNotPresent",
                          "command": ["sh", "-c", "cp /config/redpanda.yaml "
                                      "/config/.bootstrap.yaml /etc/redpanda/"],
                          "volumeMounts": [{"name": "config", "mountPath": "/config"},
                                           {"name": "etc", "mountPath": "/etc/redpanda"}]}],
                      "containers": [{
                          "name": "redpanda", "image": REDPANDA,
                          "imagePullPolicy": "IfNotPresent",
                          "args": ["redpanda", "start", "--mode", "dev-container", "--smp", "1",
                                   "--memory", "512M", "--reserve-memory", "0M",
                                   "--overprovisioned", "--default-log-level=warn",
                                   "--kafka-addr", "tls://0.0.0.0:9093",
                                   "--advertise-kafka-addr", f"tls://localhost:{port}",
                                   "--rpc-addr", "0.0.0.0:33145",
                                   "--advertise-rpc-addr", "127.0.0.1:33145"],
                          "env": [{"name": "RP_BOOTSTRAP_USER",
                                   "value": f"{TLS_ROOT[0]}:{TLS_ROOT[1]}:SCRAM-SHA-256"}],
                          "ports": [{"containerPort": 9093}, {"containerPort": 9644}],
                          "readinessProbe": {"httpGet": {"path": "/v1/status/ready",
                                                         "port": 9644}, "periodSeconds": 2},
                          "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                        "limits": {"memory": "1Gi"}},
                          "volumeMounts": [{"name": "etc", "mountPath": "/etc/redpanda"},
                                           {"name": "tls", "mountPath": "/etc/redpanda-tls",
                                            "readOnly": True}]}],
                      "volumes": [{"name": "config", "configMap": {"name": "redpanda-tls"}},
                                  {"name": "etc", "emptyDir": {}},
                                  {"name": "tls", "secret": {"secretName": "redpanda-tls",
                                                             "defaultMode": 0o444}}]}}}}]


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def kafka_tls_broker(tmp_path_factory):
    """A Kafka-API broker of the test module's own whose clients speak only TLS
    (SASL/SCRAM inside it), under a test CA; reached by port-forward."""
    pytest.importorskip("confluent_kafka")
    import base64
    import json
    import time

    from confluent_kafka import KafkaException
    from confluent_kafka.admin import AdminClient

    from kind_support import delete_namespace, kind_namespace, kubectl
    from mysql_pair_business import tls_material
    from redis_business import _forward

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the Kafka TLS tests")
    port = _free_port()
    context, namespace, _ = kind_namespace([], _tls_broker_objects(port), prefix="kafka-tls")
    forwards = []
    try:
        material = tls_material(namespace)
        kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "redpanda-tls"},
            "data": {k: base64.b64encode(v).decode() for k, v in material.items()}}))
        ca_file = tmp_path_factory.mktemp("kafka-tls") / "ca.pem"
        ca_file.write_bytes(material["ca.pem"])
        kubectl(context, "-n", namespace, "rollout", "status", "deployment/redpanda",
                "--timeout=300s")
        process, _ = _forward(context, namespace, "redpanda", 9093, local=str(port))
        forwards.append(process)
        process, api_port = _forward(context, namespace, "redpanda", 9644)
        forwards.append(process)
        address = f"localhost:{port}"
        admin = AdminClient(settings(address, *TLS_ROOT, str(ca_file)))
        deadline = time.monotonic() + 120
        while True:  # the superuser exists once the broker has bootstrapped
            try:
                admin.list_topics(timeout=5)
                break
            except KafkaException:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield {"address": address, "root": TLS_ROOT, "api": f"http://127.0.0.1:{api_port}",
               "ca_file": str(ca_file)}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


@pytest.fixture
def kafka_tls_business(kafka_tls_broker):
    b = kafka_tls_broker
    for found in business(b["address"], b["root"], b["api"], b["ca_file"]):
        yield {**found, "ca_file": b["ca_file"]}


@pytest.fixture
def kafka_cross_business(kafka_business, kafka_tls_broker):
    """The topic's business on the shared broker (source) and a second, separate
    broker - the module's own, TLS - as the target: the unit's principals
    exist there too, as an operator creates accounts, with the probes'
    transactional-ID prefix allowed; nothing else."""
    import httpx
    from confluent_kafka import Producer
    from confluent_kafka.admin import (
        AclBinding,
        AclBindingFilter,
        AclOperation,
        AclPermissionType,
        AdminClient,
        NewTopic,
        ResourcePatternType,
        ResourceType,
    )

    b, t = kafka_business, kafka_tls_broker
    target_settings = settings(t["address"], *t["root"], t["ca_file"])
    admin = AdminClient(target_settings)
    for user in b["users"].values():
        httpx.post(f"{t['api']}/v1/security/users", json={
            "username": user, "password": PASSWORD, "algorithm": "SCRAM-SHA-256"},
            timeout=10).raise_for_status()
    probe = [AclBinding(ResourceType.TRANSACTIONAL_ID, b["probe"], ResourcePatternType.PREFIXED,
                        b["principals"][k], "*", AclOperation.WRITE, AclPermissionType.ALLOW)
             for k in ("app", "batch")]
    for future in admin.create_acls(probe).values():
        future.result()
    other = b["names"]["src"].replace("orders-", "shop-")  # another tenant, target side
    admin.create_topics([NewTopic(other, 1, 1)])[other].result()
    producer = Producer(target_settings)
    producer.produce(other, value=b'{"shop": true}', partition=0)
    producer.flush(15)

    def as_user(key, side):
        if side == "source":
            return b["as_user"](key)
        return settings(t["address"], b["users"][key], PASSWORD, t["ca_file"])

    yield {**b, "target": {**t, "admin": admin, "settings": target_settings},
           "as_user_on": as_user, "other_topic": other}
    admin.delete_topics([b["names"]["tgt"], other])
    for principal in b["principals"].values():
        for future in admin.delete_acls([AclBindingFilter(
                ResourceType.ANY, None, ResourcePatternType.ANY, principal, None,
                AclOperation.ANY, AclPermissionType.ANY)]).values():
            future.result()
    admin.delete_consumer_groups([b["group"]])
    for user in b["users"].values():
        httpx.delete(f"{t['api']}/v1/security/users/{user}", timeout=10)
