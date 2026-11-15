"""Kafka-API topic handoff world for the provider-neutral conformance kit.

Observed independently of the adapter: content is read with its own
``read_committed`` consumer, positions are counted from the group's committed
offsets, access is the raw ACL list, and a write attempt is a real
transactional produce as the writer principal, always aborted (an aborted
record is never business content).
"""

import hashlib
import json
import time
from uuid import uuid4

from sova.adapters.kafka_topic_handoff import KafkaTopicHandoffAdapter


class Route:
    def __init__(self, topic):
        self.topic = topic

    def get(self):
        return self.topic

    def set(self, topic):
        self.topic = topic


class KafkaWorld:
    def __init__(self, business):
        self.b, self.names = business, business["names"]
        self.writers = {"app": "app", "other": "batch"}
        self._route = Route(self.names["src"])
        self.pending = None

    def _topic(self, side):
        return self.names["src" if side == "source" else "tgt"]

    def _adapter(self, source, target):
        p = self.b["principals"]
        return KafkaTopicHandoffAdapter(
            "kafka-topic-handoff", config=lambda side: dict(self.b["settings"]),
            source_topic=self._topic(source), target_topic=self._topic(target),
            producers=(p["app"], p["batch"]), app_producer=p["app"], route=self._route,
            consumers=(p["reader"],), groups=(self.b["group"],),
            transactional_prefixes=(self.b["prefix"],))

    def adapter(self):
        return self._adapter("source", "target")

    def reverse_adapter(self):
        return self._adapter("target", "source")

    # -- observation --------------------------------------------------------------
    def _exists(self, topic):
        return topic in self.b["admin"].list_topics(timeout=10).topics

    def _read(self, topic):
        from confluent_kafka import Consumer, KafkaError, TopicPartition

        metadata = self.b["admin"].list_topics(topic, timeout=10).topics[topic]
        consumer = Consumer({**self.b["settings"], "group.id": "world-" + uuid4().hex,
                             "enable.auto.commit": False, "isolation.level": "read_committed",
                             "enable.partition.eof": True})
        out = {p: [] for p in metadata.partitions}
        try:
            consumer.assign([TopicPartition(topic, p, -2) for p in metadata.partitions])
            todo, idle = set(metadata.partitions), 0
            while todo and idle < 20:
                m = consumer.poll(0.5)
                if m is None:
                    idle += 1
                    continue
                if m.error():
                    if m.error().code() == KafkaError._PARTITION_EOF:
                        todo.discard(m.partition())
                    continue
                kind, stamp = m.timestamp()
                out[m.partition()].append((m.offset(), m.key(), m.value(),
                                           tuple(m.headers() or ()), stamp if kind == 1 else 0))
        finally:
            consumer.close()
        return out

    def content(self, side):
        from confluent_kafka import ConsumerGroupTopicPartitions, TopicPartition

        topic = self._topic(side)
        if not self._exists(topic):
            return None
        records = self._read(topic)
        group = self.b["admin"].list_consumer_group_offsets([ConsumerGroupTopicPartitions(
            self.b["group"], [TopicPartition(topic, p) for p in records])])[
            self.b["group"]].result()
        positions = {tp.partition: sum(1 for r in records[tp.partition] if r[0] < tp.offset)
                     for tp in group.topic_partitions if tp.offset >= 0}
        h = hashlib.sha256()
        for p in sorted(records):
            for _, key, value, headers, stamp in records[p]:
                h.update(repr((p, key, value, headers, stamp)).encode())
        h.update(json.dumps(sorted(positions.items())).encode())
        return h.hexdigest()

    def _access(self, side):
        from confluent_kafka.admin import (
            AclBindingFilter,
            AclOperation,
            AclPermissionType,
            ResourcePatternType,
            ResourceType,
        )

        found = self.b["admin"].describe_acls(AclBindingFilter(
            ResourceType.ANY, None, ResourcePatternType.ANY, None, None, AclOperation.ANY,
            AclPermissionType.ANY)).result()
        topic, unit = self._topic(side), set(self.b["principals"].values())
        return sorted(f"{a.principal}:{a.restype.name}:{a.name}:{a.operation.name}:"
                      f"{a.permission_type.name}" for a in found
                      if a.principal in unit and a.name in (topic, self.b["group"],
                                                            self.b["prefix"]))

    def _open(self, side):
        from confluent_kafka import IsolationLevel, TopicPartition
        from confluent_kafka.admin import OffsetSpec

        topic = self._topic(side)
        if not self._exists(topic):
            return []
        partitions = self.b["admin"].list_topics(topic, timeout=10).topics[topic].partitions

        def ends(level):
            found = self.b["admin"].list_offsets(
                {TopicPartition(topic, p): OffsetSpec.latest() for p in partitions},
                isolation_level=level)
            return {tp.partition: f.result().offset for tp, f in found.items()}
        stable, high = ends(IsolationLevel.READ_COMMITTED), ends(IsolationLevel.READ_UNCOMMITTED)
        return sorted(p for p in partitions if stable[p] < high[p])

    def state(self):
        return (self.content("source"), self.content("target"), self._access("source"),
                self._access("target"), self._open("source"), self._route.get())

    # -- the business and writers -----------------------------------------------------
    def can_write(self, side, writer):
        from confluent_kafka import KafkaException, Producer

        key = self.writers[writer]
        producer = Producer({**self.b["as_user"](key),
                             "transactional.id": f"{self.b['probe']}{key}-{uuid4().hex[:6]}",
                             "message.timeout.ms": 10000})
        errors = []
        try:
            producer.init_transactions(15)
            producer.begin_transaction()
            producer.produce(self._topic(side), value=b"sova-probe", partition=0,
                             on_delivery=lambda error, message: errors.append(error))
            producer.flush(15)
        except KafkaException as error:
            errors.append(error.args[0])
        finally:
            try:
                producer.abort_transaction(15)
            except KafkaException:
                pass
        # KafkaError compares by code and cannot compare with None: check identity.
        return len(errors) == 1 and errors[0] is None

    def _produce(self, records):
        from confluent_kafka import Producer

        producer = Producer({**self.b["as_user"]("app"), "enable.idempotence": True})
        for key, value in records:
            producer.produce(self._topic("source"), key=key, value=value,
                             headers=[("source", b"orders-api")],
                             partition=int(key.split(b"-")[1]) % 3)
        assert producer.flush(15) == 0

    def business_write(self, count):
        start = 1000 + int(time.time() * 1000) % 100000
        self._produce([(f"order-{i}".encode(), f'{{"order": {i}}}'.encode())
                       for i in range(start, start + count)])

    def business_delete(self, count):
        """A removal in a stream: the application's tombstone for the record's key."""
        self._produce([(f"order-{i}".encode(), None) for i in range(1, count + 1)])

    def reopen_source(self):
        """The source's access restored as it was before the handoff."""
        from confluent_kafka.admin import (
            AclBinding,
            AclOperation,
            AclPermissionType,
            ResourcePatternType,
            ResourceType,
        )

        made = AclBinding(ResourceType.TOPIC, self._topic("source"),
                          ResourcePatternType.LITERAL, self.b["principals"]["app"], "*",
                          AclOperation.WRITE, AclPermissionType.ALLOW)
        for future in self.b["admin"].create_acls([made]).values():
            future.result()

    def tamper_target(self):
        from confluent_kafka import Producer

        producer = Producer(self.b["settings"])
        producer.produce(self._topic("target"), key=b"order-1", value=b'{"forged": true}',
                         partition=1)
        assert producer.flush(15) == 0

    def move_route(self, side):
        self._route.set(self._topic(side))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return self._topic(side)

    def open_pending_write(self):
        """A transaction the application opened and never finished."""
        from confluent_kafka import Producer

        self.pending = Producer({**self.b["as_user"]("app"),
                                 "transactional.id": f"{self.b['prefix']}-pending"})
        self.pending.init_transactions(15)
        self.pending.begin_transaction()
        self.pending.produce(self._topic("source"), key=b"order-pending",
                             value=b'{"pending": true}', partition=0)
        assert self.pending.flush(15) == 0

    def resolve_pending_write(self):
        """The operator fences the transactional ID: its open transaction aborts."""
        from confluent_kafka import Producer

        operator = Producer({**self.b["settings"],
                             "transactional.id": f"{self.b['prefix']}-pending"})
        operator.init_transactions(30)
        deadline = time.monotonic() + 30
        while self._open("source") and time.monotonic() < deadline:
            time.sleep(0.2)
