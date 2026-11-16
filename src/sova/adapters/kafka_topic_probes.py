"""Independent probes for a Kafka-API topic handoff (§16.3, §15.5, §28.2).

Made apart from the adapter that moved the records, by other means:

* write denial is an attempt: each registered producer principal logs in with
  its own credentials and produces one record inside a transaction that is
  always aborted (an aborted record is never business content), under a
  transactional-ID prefix registered for probes; and the broker is asked, as
  that principal, which operations it may perform on the topic (KIP-430).
  A writer is denied only when its record was not accepted and the broker
  does not authorize it to write: a failure for another reason (its own
  transactional ID refused, a timeout) decides nothing and counts against;
* content is read with a ``read_committed`` consumer of the probes' own: every
  partition's records in order (key, value, headers, producer timestamp) and
  each registered group's position counted as records before it;
* a business invariant is a registered check over the target's records: every
  value is JSON carrying a field, or no key appears twice;
* the restore drill copies the target topic into a scratch topic, compares
  it, and deletes it;
* tenant isolation is an attempt: the application principal produces to and
  reads from another tenant's topic, and must be refused both;
* transport encryption is this client's own settings: TLS with the broker's
  host name verified;
* placement comes from ``zone_of(side)``, the inventory's view, as read.

Each probe returns ``(passed, observation)``.
"""

import hashlib
import json
from uuid import uuid4

from sova.domain.contracts import ContractError


def _kafka():
    import confluent_kafka
    import confluent_kafka.admin  # noqa: F401 - the submodule, reached as k.admin

    return confluent_kafka


class KafkaTopicProbes:
    def __init__(self, *, config, config_as, topics, producers, app_producer, probe_prefix,
                 groups=(), sets=None, invariants=None, other_topic=None, zone_of=None,
                 timeout=15.0):
        """``config(side)`` gives the administrative client settings of a side's
        cluster, ``config_as(side, principal)`` a producer principal's own;
        ``topics`` maps side to topic; ``sets`` maps each protected set to a
        topic name pattern of the unit (default: the topic)."""
        if not callable(config) or not callable(config_as):
            raise ContractError("Administrative and per-principal settings required")
        if type(topics) is not dict or set(topics) != {"source", "target"}:
            raise ContractError("The topic of each side required")
        if type(probe_prefix) is not str or not probe_prefix:
            raise ContractError("A transactional-ID prefix registered for probes required")
        self.config, self.config_as, self.topics = config, config_as, topics
        self.probe_prefix = probe_prefix
        self.producers, self.app_producer = tuple(producers), app_producer
        self.groups, self.sets = tuple(groups), dict(sets or {})
        self.invariants, self.other_topic = dict(invariants or {}), other_topic
        self.zone_of, self.timeout = zone_of, timeout

    # -- writes ------------------------------------------------------------------
    def _try_produce(self, side, principal, topic):
        """(wrote, outcome): ``write_accepted``, ``write_refused`` (the topic or the
        login refused) or ``undecided:<error>``."""
        k = _kafka()
        settings = self.config_as(side, principal)
        producer = k.Producer({**settings,
                               "transactional.id": f"{self.probe_prefix}{uuid4().hex[:12]}",
                               "message.timeout.ms": int(self.timeout * 1000)})
        errors = []
        try:
            producer.init_transactions(self.timeout)
            producer.begin_transaction()
            producer.produce(topic, value=b"sova-probe", partition=0,
                             on_delivery=lambda error, message: errors.append(error))
            producer.flush(self.timeout)
        except k.KafkaException as error:
            errors.append(error.args[0])
        finally:
            try:
                producer.abort_transaction(self.timeout)
            except k.KafkaException:
                pass
        # KafkaError compares by code and not with None: check identity.
        if len(errors) == 1 and errors[0] is None:
            return True, "write_accepted"
        failed = [e for e in errors if e is not None]
        codes = {e.code() for e in failed}
        if codes & {k.KafkaError.TOPIC_AUTHORIZATION_FAILED,
                    k.KafkaError.SASL_AUTHENTICATION_FAILED, k.KafkaError._AUTHENTICATION}:
            return False, "write_refused"
        return False, "undecided:" + (failed[0].name() if failed else "no_answer")

    def _authorized(self, side, principal, topic):
        """What the broker authorizes ``principal`` to do on ``topic``, asked as it."""
        k = _kafka()
        admin = k.admin.AdminClient(self.config_as(side, principal))
        future = admin.describe_topics(k.TopicCollection([topic]),
                                       include_authorized_operations=True)[topic]
        try:
            found = future.result(timeout=self.timeout)
        except k.KafkaException as error:
            if error.args[0].code() == k.KafkaError.TOPIC_AUTHORIZATION_FAILED:
                return set()  # not even allowed to describe it
            raise
        return {o.name for o in found.authorized_operations or ()}

    def _denied(self, side, principal, topic):
        wrote, outcome = self._try_produce(side, principal, topic)
        operations = self._authorized(side, principal, topic)
        may = bool(operations & {"WRITE", "ALL"})
        return (not wrote and not may), {"attempt": outcome,
                                         "authorized": sorted(operations)}

    def write_denied(self, side):
        outcomes = {p: self._denied(side, p, self.topics[side]) for p in self.producers}
        return all(denied for denied, _ in outcomes.values()), {
            "probe": "write_denied", "side": side, "topic": self.topics[side],
            "outcomes": {p: detail for p, (_, detail) in outcomes.items()}}

    def single_writer(self):
        wrote, target = self._try_produce("target", self.app_producer, self.topics["target"])
        denied, source = self.write_denied("source")
        return wrote and denied, {"probe": "single_writer", "target": target,
                                  "source": source["outcomes"]}

    # -- content -----------------------------------------------------------------
    def _read(self, side, topic):
        """partition -> [(offset, key, value, headers, producer timestamp)], committed."""
        k = _kafka()
        settings = self.config(side)
        admin = k.admin.AdminClient(settings)
        described = admin.list_topics(topic, timeout=self.timeout).topics.get(topic)
        if described is None or described.error is not None:
            return None
        consumer = k.Consumer({**settings, "group.id": f"sova-probe-{uuid4().hex}",
                               "enable.auto.commit": False, "isolation.level": "read_committed",
                               "enable.partition.eof": True})
        out = {p: [] for p in described.partitions}
        try:
            consumer.assign([k.TopicPartition(topic, p, k.OFFSET_BEGINNING)
                             for p in described.partitions])
            todo, idle = set(described.partitions), 0
            while todo and idle < 20:
                m = consumer.poll(0.5)
                if m is None:
                    idle += 1
                    continue
                if m.error():
                    if m.error().code() == k.KafkaError._PARTITION_EOF:
                        todo.discard(m.partition())
                    continue
                kind, stamp = m.timestamp()
                out[m.partition()].append((m.offset(), m.key(), m.value(),
                                           tuple(m.headers() or ()), stamp if kind == 1 else 0))
            if todo:
                raise ContractError(f"Partitions not read to their end: {sorted(todo)}")
        finally:
            consumer.close()
        return out

    def _positions(self, side, topic, records):
        k = _kafka()
        admin = k.admin.AdminClient(self.config(side))
        positions = {}
        for group in self.groups:
            found = admin.list_consumer_group_offsets([k.ConsumerGroupTopicPartitions(
                group, [k.TopicPartition(topic, p) for p in records])])[group].result()
            positions[group] = sorted(
                (tp.partition, sum(1 for r in records[tp.partition] if r[0] < tp.offset))
                for tp in found.topic_partitions if tp.offset >= 0)
        return positions

    def _digest(self, side, topic):
        records = self._read(side, topic)
        if records is None:
            return None, 0
        h, count = hashlib.sha256(), 0
        for partition in sorted(records):
            for _, key, value, headers, stamp in records[partition]:
                h.update(repr((partition, key, value, headers, stamp)).encode())
                count += 1
        h.update(json.dumps(self._positions(side, topic, records)).encode())
        return h.hexdigest(), count

    def content_equal(self, name):
        if self.sets and name not in self.sets:
            raise ContractError("Unregistered protected set")
        source = self._digest("source", self.topics["source"])
        target = self._digest("target", self.topics["target"])
        return source == target and source[0] is not None, {
            "probe": "content_equal", "set": name,
            "source": {"records": source[1], "digest": source[0]},
            "target": {"records": target[1], "digest": target[0]}}

    def invariant(self, invariant_id):
        spec = self.invariants.get(invariant_id)
        if spec is None:
            return False, {"probe": "invariant", "invariant": invariant_id,
                           "reason": "no probe registered for this invariant"}
        records = self._read("target", self.topics["target"]) or {}
        flat = [r for p in sorted(records) for r in records[p]]
        if spec.get("type") == "json_field":
            bad = []
            for offset, _, value, _, _ in flat:
                if value is None:
                    continue  # a tombstone removes; it carries no business record
                try:
                    ok = spec["field"] in json.loads(value)
                except (ValueError, TypeError):
                    ok = False
                if not ok:
                    bad.append(offset)
            return bool(flat) and not bad, {"probe": "invariant", "invariant": invariant_id,
                                            "records": len(flat), "violations": bad[:20]}
        if spec.get("type") == "unique_keys":
            keys = [r[1] for r in flat if r[2] is not None]
            twice = sorted({k.decode(errors="replace") for k in keys if keys.count(k) > 1})
            return bool(keys) and not twice, {"probe": "invariant", "invariant": invariant_id,
                                              "records": len(keys), "repeated": twice[:20]}
        raise ContractError("Unknown invariant type")

    # -- isolation, transport, placement ---------------------------------------------------
    def cross_tenant_denied(self, other=None):
        k = _kafka()
        topic = other or self.other_topic
        if not topic:
            return False, {"probe": "cross_tenant_denied", "reason": "no other tenant registered"}
        denied, write = self._denied("target", self.app_producer, topic)
        consumer = k.Consumer({**self.config_as("target", self.app_producer),
                               "group.id": f"sova-probe-{uuid4().hex}",
                               "enable.auto.commit": False})
        try:
            consumer.assign([k.TopicPartition(topic, 0, k.OFFSET_BEGINNING)])
            read = "no_answer"
            for _ in range(20):
                m = consumer.poll(0.5)
                if m is None:
                    continue
                if m.error():
                    read = m.error().name()
                    break
                read = "record_read"
                break
        finally:
            consumer.close()
        return denied and read != "record_read", {
            "probe": "cross_tenant_denied", "other_topic": topic, "write": write, "read": read}

    def transport_encrypted(self, side):
        settings = self.config(side)
        tls = settings.get("security.protocol", "PLAINTEXT").upper() in ("SSL", "SASL_SSL")
        verified = tls and settings.get("ssl.endpoint.identification.algorithm",
                                        "https") == "https" and \
            settings.get("enable.ssl.certificate.verification", True) not in (False, "false")
        return verified, {"probe": "transport_encrypted", "side": side, "tls": tls,
                          "client_verification": "verify-identity" if verified else "none"}

    def zone(self, side, allowed):
        label = self.zone_of(side) if self.zone_of else None
        return label in allowed, {"probe": "zone", "side": side, "label": label,
                                  "allowed": sorted(allowed),
                                  "source": "inventory" if self.zone_of else "none"}

    # -- restore -----------------------------------------------------------------------
    def restore_drill(self, scratch=None):
        """Copy the target topic into a new scratch topic on the target cluster,
        compare, and delete the scratch topic."""
        k = _kafka()
        topic = self.topics["target"]
        scratch = f"{scratch or 'sova-restore'}-{uuid4().hex[:8]}"
        settings = self.config("target")
        admin = k.admin.AdminClient(settings)
        records = self._read("target", topic)
        if records is None:
            return False, {"probe": "restore_drill", "reason": "target topic missing"}
        try:
            admin.create_topics([k.admin.NewTopic(scratch, len(records), 1)])[scratch].result()
            producer = k.Producer({**settings, "enable.idempotence": True})
            for partition in sorted(records):
                for _, key, value, headers, stamp in records[partition]:
                    producer.produce(scratch, key=key, value=value, headers=list(headers),
                                     partition=partition, **({"timestamp": stamp}
                                                             if stamp else {}))
            if producer.flush(self.timeout):
                return False, {"probe": "restore_drill", "reason": "records not delivered"}
            restored = self._read("target", scratch)
            strip = {p: [r[1:] for r in rs] for p, rs in records.items()}
            equal = restored is not None and {p: [r[1:] for r in rs]
                                              for p, rs in restored.items()} == strip
        finally:
            admin.delete_topics([scratch])
        return equal, {"probe": "restore_drill", "scratch_topic": scratch,
                       "records": sum(len(r) for r in records.values())}
