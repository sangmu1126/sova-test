"""Reference adapter: a Kafka-API topic handed off between two clusters (§16.1, §16.5).

The unit is one topic with its producers, its consumers and their consumer
groups' positions. The source and target are Kafka-API clusters (Apache
Kafka, Redpanda, managed services) reached with an administrative principal;
the unit's principals are governed by ACLs. Every postcondition is read back
from the clusters:

* stage_target - the target topic exists with the source's partition count
  and dynamic topic configuration, and no unit principal has any write or
  read access to it or to the unit's groups;
* pause_admission - no producer principal may write the source topic;
* fence_source_writes - no producer may write the topic or use the unit's
  transactional IDs, no consumer may read the topic or commit to the unit's
  groups, the groups have no live member, and no transaction is open on any
  partition (the last stable offset equals the high watermark): an open
  transaction could still commit after the fence;
* start_sync / copy_content / apply_final_changes - the target topic holds,
  partition by partition and in order, exactly the source's committed
  records (key, value, headers, and the timestamp when the producer set it),
  and each group's committed position is translated: a consumer resumes on
  the target at the same record it would have read next on the source;
* verify_final_state - the same comparison, configuration included;
* activate_target - only the application producer may write the target,
  consumers may read it and commit to their groups, the source stays fenced;
* commit_route - the route points at the target topic.

Kafka authorizes every request against the current ACLs, so revoking is
enough for open connections; nothing caches a right as MySQL sessions do.

The copy is idempotent: it first deletes the target's records
(``DeleteRecords``) and rewrites every committed source record in one
transaction per partition, so a copy cut short leaves nothing visible to a
``read_committed`` reader. Offsets differ between clusters; positions are
compared as "the number of records before it". Records a producer aborted
are not business content and are not copied. ACLs are evaluated as the
standard authorizer does: literal, prefixed and wildcard patterns, the
``User:*`` principal, ``ALL``, and DENY over ALLOW.
"""

from bisect import bisect_left
import hashlib
import json
from uuid import uuid4

from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})


def _kafka():
    from confluent_kafka import (
        Consumer,
        ConsumerGroupTopicPartitions,
        IsolationLevel,
        KafkaError,
        KafkaException,
        Producer,
        TopicCollection,
        TopicPartition,
    )
    from confluent_kafka import admin

    return {"Consumer": Consumer, "Producer": Producer, "TopicPartition": TopicPartition,
            "TopicCollection": TopicCollection, "IsolationLevel": IsolationLevel,
            "KafkaError": KafkaError, "KafkaException": KafkaException,
            "Groups": ConsumerGroupTopicPartitions, "admin": admin}


class KafkaTopicHandoffAdapter:
    def __init__(self, adapter_id, *, config, source_topic, target_topic, producers,
                 app_producer, route, consumers=(), groups=(), transactional_prefixes=(),
                 timeout=15.0):
        """``config(side)`` gives the librdkafka settings of an administrative
        principal on that side's cluster (``"source"`` / ``"target"``).
        Principals are ``User:<name>``."""
        if not callable(config) or not all(callable(getattr(route, n, None))
                                           for n in ("get", "set")):
            raise ContractError("Cluster settings and a route store required")
        self.adapter_id, self.config, self.route = adapter_id, config, route
        self.source_topic, self.target_topic = source_topic, target_topic
        self.producers = tuple(producers)
        if app_producer not in self.producers:
            raise ContractError("The application producer must be a registered writer")
        if not all(p.startswith("User:") for p in self.producers + tuple(consumers)):
            raise ContractError("Principals are User:<name>")
        self.app_producer, self.consumers = app_producer, tuple(consumers)
        self.groups, self.prefixes = tuple(groups), tuple(transactional_prefixes)
        self.timeout = timeout
        self._admins = {}

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported handoff action for this adapter")

    def apply(self, command):
        getattr(self, "_" + command.action)()

    def readback(self, command, now):
        holds, detail = getattr(self, "_check_" + command.action)()
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **detail}, limit=65536))

    # -- clusters ----------------------------------------------------------------
    def _topic(self, side):
        return self.source_topic if side == "source" else self.target_topic

    def _admin(self, side):
        """One administrative client per side, kept while the adapter lives: a
        client dropped before its futures resolve cancels them."""
        if side not in self._admins:
            self._admins[side] = _kafka()["admin"].AdminClient(self.config(side))
        return self._admins[side]

    def _describe(self, side):
        """Partition count of the side's topic, or None when it does not exist."""
        k = _kafka()
        try:
            found = self._admin(side).describe_topics(k["TopicCollection"](
                [self._topic(side)]), request_timeout=self.timeout)[self._topic(side)].result()
        except k["KafkaException"]:
            return None
        return len(found.partitions)

    def _configs(self, side):
        admin = _kafka()["admin"]
        resource = admin.ConfigResource(admin.ResourceType.TOPIC, self._topic(side))
        entries = self._admin(side).describe_configs([resource])[resource].result()
        return {n: e.value for n, e in sorted(entries.items())
                if e.source == admin.ConfigSource.DYNAMIC_TOPIC_CONFIG.value}

    def _offsets(self, side, partitions, level):
        k = _kafka()
        spec = k["admin"].OffsetSpec.latest()
        found = self._admin(side).list_offsets(
            {k["TopicPartition"](self._topic(side), p): spec for p in range(partitions)},
            isolation_level=level, request_timeout=self.timeout)
        return {tp.partition: f.result().offset for tp, f in found.items()}

    def _open_transactions(self, side, partitions):
        """Partitions whose last stable offset is behind the high watermark."""
        k = _kafka()
        stable = self._offsets(side, partitions, k["IsolationLevel"].READ_COMMITTED)
        high = self._offsets(side, partitions, k["IsolationLevel"].READ_UNCOMMITTED)
        return sorted(p for p in range(partitions) if stable[p] < high[p])

    def _records(self, side, partitions):
        """Every committed record per partition, in order: [(offset, record)]."""
        k = _kafka()
        topic = self._topic(side)
        consumer = k["Consumer"]({**self.config(side), "group.id": "sova-readback-" +
                                  uuid4().hex, "enable.auto.commit": False,
                                  "isolation.level": "read_committed",
                                  "enable.partition.eof": True, "auto.offset.reset": "earliest"})
        try:
            starts = self._admin(side).list_offsets(
                {k["TopicPartition"](topic, p): k["admin"].OffsetSpec.earliest()
                 for p in range(partitions)}, request_timeout=self.timeout)
            low = {tp.partition: f.result().offset for tp, f in starts.items()}
            stable = self._offsets(side, partitions, k["IsolationLevel"].READ_COMMITTED)
            out = {p: [] for p in range(partitions)}
            todo = {p for p in range(partitions) if low[p] < stable[p]}
            consumer.assign([k["TopicPartition"](topic, p, low[p]) for p in todo])
            idle = 0
            while todo and idle < 20:
                message = consumer.poll(0.5)
                if message is None:
                    idle += 1
                    continue
                if message.error():
                    if message.error().code() == k["KafkaError"]._PARTITION_EOF:
                        todo.discard(message.partition())
                        continue
                    raise ContractError(f"Reading {topic} failed: {message.error().name()}")
                idle = 0
                kind, stamp = message.timestamp()
                out[message.partition()].append((message.offset(), {
                    "key": message.key(), "value": message.value(),
                    "headers": list(message.headers() or ()),
                    "timestamp": stamp if kind == 1 else None}))  # 1: set by the producer
                if message.offset() + 1 >= stable[message.partition()]:
                    todo.discard(message.partition())
            if todo:
                raise ContractError(f"Reading {topic} did not reach the end of {sorted(todo)}")
            return out
        finally:
            consumer.close()

    @staticmethod
    def _digest(records):
        h = hashlib.sha256()
        for _, r in records:
            text = json.dumps([_bytes(r["key"]), _bytes(r["value"]),
                               [[n, _bytes(v)] for n, v in r["headers"]], r["timestamp"]],
                              separators=(",", ":"))
            h.update(len(text).to_bytes(8, "big") + text.encode())
        return {"records": len(records), "sha256": h.hexdigest()}

    def _committed(self, side, partitions):
        """group -> partition -> committed offset on the side's topic."""
        k = _kafka()
        out = {}
        for group in self.groups:
            request = k["Groups"](group, [k["TopicPartition"](self._topic(side), p)
                                          for p in range(partitions)])
            found = self._admin(side).list_consumer_group_offsets(
                [request], request_timeout=self.timeout)[group].result()
            out[group] = {tp.partition: tp.offset for tp in found.topic_partitions
                          if tp.offset >= 0}
        return out

    def _positions(self, side, partitions, records=None):
        """group -> partition -> number of committed records before its position."""
        records = records or self._records(side, partitions)
        offsets = {p: [o for o, _ in rs] for p, rs in records.items()}
        return {group: {p: bisect_left(offsets[p], offset) for p, offset in by_p.items()}
                for group, by_p in self._committed(side, partitions).items()}

    def _members(self, side):
        if not self.groups:
            return {}
        found = self._admin(side).describe_consumer_groups(list(self.groups),
                                                           request_timeout=self.timeout)
        out = {}
        for group, future in found.items():
            try:
                out[group] = len(future.result().members)
            except _kafka()["KafkaException"]:
                out[group] = 0  # no such group: nobody in it
        return out

    # -- access ------------------------------------------------------------------
    def _acls(self, side):
        admin = _kafka()["admin"]
        query = admin.AclBindingFilter(admin.ResourceType.ANY, None,
                                       admin.ResourcePatternType.ANY, None, None,
                                       admin.AclOperation.ANY, admin.AclPermissionType.ANY)
        return self._admin(side).describe_acls(query, request_timeout=self.timeout).result()

    @staticmethod
    def _allowed(acls, principal, resource, name, operation):
        """(allowed, the bindings that decided it) as the standard authorizer decides."""
        admin = _kafka()["admin"]
        implied = {operation, admin.AclOperation.ALL}
        if operation == admin.AclOperation.DESCRIBE:
            implied |= {admin.AclOperation.READ, admin.AclOperation.WRITE,
                        admin.AclOperation.DELETE, admin.AclOperation.ALTER}

        def matches(b):
            if b.restype != resource or b.principal not in (principal, "User:*") or \
                    b.operation not in implied:
                return False
            if b.resource_pattern_type == admin.ResourcePatternType.PREFIXED:
                return name.startswith(b.name)
            return b.name in (name, "*")
        hits = [b for b in acls if matches(b)]
        deny = [b for b in hits if b.permission_type == admin.AclPermissionType.DENY]
        allow = [b for b in hits if b.permission_type == admin.AclPermissionType.ALLOW]
        decided = deny or allow
        return bool(allow) and not deny, [
            f"{b.permission_type.name}:{b.operation.name}:{b.restype.name}:"
            f"{b.resource_pattern_type.name}:{b.name}:{b.principal}" for b in decided][:8]

    def _access(self, side, *, cluster_wide=False):
        """What each unit principal may still do on the side's topic and, with
        ``cluster_wide``, with the unit's groups and transactional IDs. Those
        two are resources of the whole cluster, not of a topic: neither lets a
        principal write or commit to a topic it has no right on, so they are
        judged where they matter - all of them when the source is fenced, the
        groups when the target is activated - and not when another topic of a
        shared cluster is staged."""
        admin = _kafka()["admin"]
        acls, topic = self._acls(side), self._topic(side)
        R, Op = admin.ResourceType, admin.AclOperation
        out = {}
        for p in self.producers:
            allowed, why = self._allowed(acls, p, R.TOPIC, topic, Op.WRITE)
            out[f"{p}:write"] = why if allowed else []
            for prefix in self.prefixes if cluster_wide else ():
                allowed, why = self._allowed(acls, p, R.TRANSACTIONAL_ID, prefix, Op.WRITE)
                out[f"{p}:transactional:{prefix}"] = why if allowed else []
        for c in self.consumers:
            allowed, why = self._allowed(acls, c, R.TOPIC, topic, Op.READ)
            out[f"{c}:read"] = why if allowed else []
            for group in self.groups if cluster_wide else ():
                allowed, why = self._allowed(acls, c, R.GROUP, group, Op.READ)
                out[f"{c}:group:{group}"] = why if allowed else []
        return out

    def _revoke(self, side, principals, resource, name, operations):
        admin = _kafka()["admin"]
        filters = [admin.AclBindingFilter(resource, name, admin.ResourcePatternType.LITERAL, p,
                                          None, op, admin.AclPermissionType.ALLOW)
                   for p in principals for op in operations]
        if filters:
            for future in self._admin(side).delete_acls(
                    filters, request_timeout=self.timeout).values():
                future.result()

    def _grant(self, side, bindings):
        """``bindings``: (resource, name, principal, operation[, pattern type])."""
        admin = _kafka()["admin"]
        made = [admin.AclBinding(b[0], b[1], b[4] if len(b) > 4 else
                                 admin.ResourcePatternType.LITERAL, b[2], "*", b[3],
                                 admin.AclPermissionType.ALLOW)
                for b in bindings]
        for future in self._admin(side).create_acls(made, request_timeout=self.timeout).values():
            future.result()

    def _close_unit(self, side, *, cluster_wide=False):
        """Remove every literal grant of the unit's principals on the side's
        topic, and with ``cluster_wide`` on its groups and transactional IDs."""
        admin = _kafka()["admin"]
        R, Op = admin.ResourceType, admin.AclOperation
        topic = self._topic(side)
        self._revoke(side, self.producers, R.TOPIC, topic, (Op.WRITE, Op.ALL))
        for prefix in self.prefixes if cluster_wide else ():
            filters = [admin.AclBindingFilter(R.TRANSACTIONAL_ID, prefix,
                                              admin.ResourcePatternType.ANY, p, None, op,
                                              admin.AclPermissionType.ALLOW)
                       for p in self.producers for op in (Op.WRITE, Op.ALL)]
            for future in self._admin(side).delete_acls(filters).values():
                future.result()
        self._revoke(side, self.consumers, R.TOPIC, topic, (Op.READ, Op.ALL))
        for group in self.groups if cluster_wide else ():
            self._revoke(side, self.consumers, R.GROUP, group, (Op.READ, Op.ALL))

    # -- actions -------------------------------------------------------------------
    def _stage_target(self):
        admin = _kafka()["admin"]
        partitions = self._describe("source")
        if partitions is None:
            raise ContractError("The source topic does not exist")
        if self._describe("target") is None:
            topic = admin.NewTopic(self.target_topic, partitions, -1,
                                   config=self._configs("source"))
            self._admin("target").create_topics([topic])[self.target_topic].result()
        self._close_unit("target")

    def _check_stage_target(self):
        source, target = self._describe("source"), self._describe("target")
        if target is None:
            return False, {"target_topic": False}
        configs = self._configs("source"), self._configs("target")
        access = self._access("target")
        return (source == target and configs[0] == configs[1]
                and not any(access.values())), {
            "partitions": [source, target], "same_configuration": configs[0] == configs[1],
            "target_access": access}

    def _pause_admission(self):
        admin = _kafka()["admin"]
        self._revoke("source", self.producers, admin.ResourceType.TOPIC, self.source_topic,
                     (admin.AclOperation.WRITE, admin.AclOperation.ALL))

    def _check_pause_admission(self):
        access = self._access("source")
        writes = {k: v for k, v in access.items() if k.endswith(":write")}
        return not any(writes.values()), {"source_writes": writes}

    def _fence_source_writes(self):
        self._close_unit("source", cluster_wide=True)

    def _check_fence_source_writes(self):
        partitions = self._describe("source") or 0
        access = self._access("source", cluster_wide=True)
        members = self._members("source")
        open_ = self._open_transactions("source", partitions)
        return not any(access.values()) and not any(members.values()) and not open_, {
            "source_access": access, "group_members": members,
            "open_transaction_partitions": open_}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        present, detail = self._check_stage_target()
        open_ = self._open_transactions("target", self._describe("target") or 0)
        return present and not open_, {**detail, "open_transaction_partitions": open_}

    def _copy(self):
        k = _kafka()
        partitions = self._describe("source")
        target_partitions = self._describe("target")
        if partitions is None or target_partitions != partitions:
            raise ContractError("Source and target topics must exist with equal partitions")
        source = self._records("source", partitions)
        # Logically empty the target: its records before the high watermark go.
        high = self._offsets("target", partitions, k["IsolationLevel"].READ_UNCOMMITTED)
        doomed = [k["TopicPartition"](self.target_topic, p, high[p])
                  for p in range(partitions) if high[p] > 0]
        if doomed:
            for future in self._admin("target").delete_records(
                    doomed, request_timeout=self.timeout).values():
                future.result()
        producer = k["Producer"]({**self.config("target"),
                                  "transactional.id": f"sova-copy-{self.target_topic}",
                                  "enable.idempotence": True})
        producer.init_transactions(self.timeout)
        for p in range(partitions):
            if not source[p]:
                continue
            producer.begin_transaction()
            for _, r in source[p]:
                extra = {"timestamp": r["timestamp"]} if r["timestamp"] is not None else {}
                while True:
                    try:
                        producer.produce(self.target_topic, value=r["value"], key=r["key"],
                                         partition=p, headers=r["headers"] or None, **extra)
                        break
                    except BufferError:  # the local queue is full: let it drain
                        producer.poll(0.5)
            producer.commit_transaction(self.timeout)
        producer.flush(self.timeout)
        # Each group resumes at the same record: translate by position.
        target = self._records("target", partitions)
        positions = self._positions("source", partitions, source)
        end = self._offsets("target", partitions, k["IsolationLevel"].READ_COMMITTED)
        requests = []
        for group, by_p in positions.items():
            moved = [k["TopicPartition"](self.target_topic, p,
                                         target[p][n][0] if n < len(target[p]) else end[p])
                     for p, n in by_p.items()]
            if moved:
                requests.append(k["Groups"](group, moved))
        for request in requests:
            self._admin("target").alter_consumer_group_offsets(
                [request], request_timeout=self.timeout)[request.group_id].result()

    def _compare(self):
        partitions = self._describe("source")
        target_partitions = self._describe("target")
        if partitions is None or target_partitions != partitions:
            return False, {"partitions": [partitions, target_partitions]}
        source, target = self._records("source", partitions), self._records("target",
                                                                            partitions)
        digests = {str(p): [self._digest(source[p]), self._digest(target[p])]
                   for p in range(partitions)}
        positions = (self._positions("source", partitions, source),
                     self._positions("target", partitions, target))
        same = all(a == b for a, b in digests.values()) and positions[0] == positions[1]
        return same, {"partitions": digests, "group_positions": [
            {g: {str(p): n for p, n in by_p.items()} for g, by_p in side.items()}
            for side in positions]}

    def _start_sync(self):
        self._copy()

    def _check_start_sync(self):
        return self._compare()

    def _copy_content(self):
        self._copy()

    def _check_copy_content(self):
        return self._compare()

    def _apply_final_changes(self):
        self._copy()

    def _check_apply_final_changes(self):
        return self._compare()

    def _verify_final_state(self):
        return None  # measurement only; the readback is the comparison

    def _check_verify_final_state(self):
        same, detail = self._compare()
        if not same:
            return False, detail
        configs = self._configs("source"), self._configs("target")
        return configs[0] == configs[1], {**detail,
                                          "same_configuration": configs[0] == configs[1]}

    def _activate_target(self):
        admin = _kafka()["admin"]
        R, Op = admin.ResourceType, admin.AclOperation
        topic = self.target_topic
        self._grant("target", [(R.TOPIC, topic, self.app_producer, Op.WRITE),
                               (R.TOPIC, topic, self.app_producer, Op.DESCRIBE)]
                    + [(R.TRANSACTIONAL_ID, prefix, self.app_producer, Op.WRITE,
                        admin.ResourcePatternType.PREFIXED) for prefix in self.prefixes]
                    + [(R.TOPIC, topic, c, Op.READ) for c in self.consumers]
                    + [(R.GROUP, g, c, Op.READ) for c in self.consumers for g in self.groups])

    def _check_activate_target(self):
        target, source = self._access("target", cluster_wide=True), self._access("source")
        mine = f"{self.app_producer}:"
        app = bool(target.get(mine + "write"))
        others = {k: v for k, v in target.items() if v and not k.startswith(mine)
                  and (k.endswith(":write") or ":transactional:" in k)}
        readers = all(v for k, v in target.items() if k.endswith(":read") or ":group:" in k)
        return app and not others and readers and not any(source.values()), {
            "target_access": target, "source_access": source}

    def _commit_route(self):
        self.route.set(self.target_topic)

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.target_topic, {"route": current}


def _bytes(value):
    return None if value is None else bytes(value).hex()
