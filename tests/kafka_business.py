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


def settings(address, user, password):
    return {"bootstrap.servers": address, "security.protocol": "SASL_PLAINTEXT",
            "sasl.mechanism": "SCRAM-SHA-256", "sasl.username": user,
            "sasl.password": password}


@pytest.fixture
def kafka_business():
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

    address, root, api = broker()
    suffix = uuid4().hex[:8]
    names = {"src": f"orders-{suffix}-src", "tgt": f"orders-{suffix}-tgt"}
    users = {k: f"orders-{k}-{suffix}" for k in ("app", "batch", "reader")}
    principals = {k: f"User:{u}" for k, u in users.items()}
    group, prefix, probe = f"orders-consumers-{suffix}", f"orders-app-{suffix}-tx", \
        f"sova-probe-{suffix}-"
    admin_settings = settings(address, *root)
    admin = AdminClient(admin_settings)
    for user in users.values():
        httpx.post(f"{api}/v1/security/users", json={
            "username": user, "password": PASSWORD, "algorithm": "SCRAM-SHA-256"},
            timeout=10).raise_for_status()
    created = admin.create_topics([NewTopic(names["src"], PARTITIONS, 1, config={
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
        return settings(address, users[key], PASSWORD)

    producer = Producer({**as_user("app"), "enable.idempotence": True})
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
           "as_user": as_user, "TopicPartition": TopicPartition}
    admin.delete_topics([n for n in names.values()])
    for principal in principals.values():
        admin.delete_acls([AclBindingFilter(ResourceType.ANY, None, ResourcePatternType.ANY,
                                            principal, None, AclOperation.ANY,
                                            AclPermissionType.ANY)])
    admin.delete_consumer_groups([group])
    for user in users.values():
        httpx.delete(f"{api}/v1/security/users/{user}", timeout=10)
