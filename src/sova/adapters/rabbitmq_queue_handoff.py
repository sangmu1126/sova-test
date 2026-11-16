"""Reference adapter: a RabbitMQ queue handed off between two virtual hosts or
brokers (§16.1, §16.5).

The unit is one durable queue, the exchange that feeds it and its binding, the
publishing users and the consuming users of the source virtual host. Every
postcondition is read back from the brokers, with the management API for
definitions, permissions, connections and shovels, and with AMQP for message
counts (the management API's counts lag by its statistics interval):

* stage_target - the target has the queue (same type, durability and
  arguments), the exchange and the binding, and no unit user may use the
  target virtual host;
* pause_admission - no publisher may write to the source virtual host;
* fence_source_writes - no publisher may write or configure, no consumer may
  read, no unit user has a connection left, and nothing outside a client
  writes into the source: no shovel delivers into it and no federation
  upstream is defined there. RabbitMQ caches permission checks per channel,
  so revoking alone would leave open channels writing; the fence closes every
  connection of the unit's users. Connections are listed and closed through
  the broker's connection tracking (``/connections/username/<user>``): the
  management API's ordinary connection list lags by its statistics interval
  (measured: a new connection appeared after 3.8 s, and closing it by name
  failed until then). Closing by user ends that user's connections to every
  virtual host of the broker: the unit's users are its own. A closing
  connection stays tracked until its client confirms the close, or for the
  broker's closing timeout (30 s) when the client does not answer; frames it
  sends meanwhile are discarded, and the fence waits for it to go;
* start_sync / copy_content / apply_final_changes - the target queue holds as
  many messages as the source, each a copy confirmed by the target broker;
* verify_final_state - definitions and message counts are equal;
* activate_target - only the application publisher may write the exchange,
  consumers may read the queue, the source stays fenced;
* commit_route - the route points at the target virtual host.

A message cannot be read without changing it: a delivery that is returned is
marked redelivered, and a quorum queue counts it towards its delivery limit.
So readbacks compare definitions and counts, never contents. The copy reads
every source message without acknowledging it, publishes a copy to the
target queue with publisher confirms, and only then returns all of them to
the source in their order: the source keeps its messages (for failback and
for the count comparison) and they are marked redelivered there, where no
consumer reads them any more. The target is purged first, so a copy repeated
or cut short leaves the same result. Content equality is established at the
copy (each message read is the message confirmed), not measured later. A
message's ``expiration`` starts again on the target; the broker-added
``x-delivery-count`` header is not copied.
"""

import time
from urllib.parse import quote, unquote, urlsplit

from sova.adapters.evidence_bounds import bounded
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
NOTHING = "^$"
_BROKER_HEADERS = frozenset({"x-delivery-count"})


class RabbitMqQueueHandoffAdapter:
    def __init__(self, adapter_id, *, broker, source_vhost, target_vhost, queue, exchange,
                 routing_key, publishers, app_publisher, consumers, route, admin_user,
                 timeout=10.0, close_wait_seconds=35.0):
        """``broker(side)`` gives ``{"http": <management API base>, "amqp": <pika
        ConnectionParameters factory: vhost -> parameters>, "auth": (user, password)}``
        for an administrative user of that side's broker."""
        if not callable(broker) or not all(callable(getattr(route, n, None))
                                           for n in ("get", "set")):
            raise ContractError("Broker access and a route store required")
        if app_publisher not in publishers:
            raise ContractError("The application publisher must be a registered writer")
        self.adapter_id, self.broker, self.route = adapter_id, broker, route
        self.source_vhost, self.target_vhost = source_vhost, target_vhost
        self.queue, self.exchange, self.routing_key = queue, exchange, routing_key
        self.publishers, self.app_publisher = tuple(publishers), app_publisher
        self.consumers, self.admin_user = tuple(consumers), admin_user
        self.users = tuple(dict.fromkeys(self.publishers + self.consumers))
        self.timeout, self.close_wait_seconds = timeout, close_wait_seconds

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
            canonical_json({"action": command.action, **bounded(detail)}, limit=8192))

    # -- brokers -----------------------------------------------------------------
    def _vhost(self, side):
        return self.source_vhost if side == "source" else self.target_vhost

    def _http(self, side, method, path, body=None, *, missing_ok=False):
        import httpx

        b = self.broker(side)
        response = httpx.request(method, b["http"].rstrip("/") + "/api" + path, json=body,
                                 auth=b["auth"], timeout=self.timeout)
        if response.status_code == 404 and missing_ok:
            return None
        response.raise_for_status()
        return response.json() if response.content else None

    def _v(self, side):
        return quote(self._vhost(side), safe="")

    def _connect(self, side):
        import pika

        return pika.BlockingConnection(self.broker(side)["amqp"](self._vhost(side)))

    def _count(self, side):
        """(ready messages, consumers) of the side's queue, now, or None."""
        import pika

        if self._http(side, "GET", f"/vhosts/{self._v(side)}", missing_ok=True) is None:
            return None
        try:
            connection = self._connect(side)
        except pika.exceptions.AMQPConnectionError:
            return None
        try:
            channel = connection.channel()
            try:
                found = channel.queue_declare(self.queue, passive=True).method
            except pika.exceptions.ChannelClosedByBroker:
                return None
            return found.message_count, found.consumer_count
        finally:
            if connection.is_open:
                connection.close()

    def _definitions(self, side):
        v = self._v(side)
        queue = self._http(side, "GET", f"/queues/{v}/{quote(self.queue, safe='')}",
                           missing_ok=True)
        exchange = self._http(side, "GET", f"/exchanges/{v}/{quote(self.exchange, safe='')}",
                              missing_ok=True)
        if queue is None or exchange is None:
            return None
        bindings = self._http(side, "GET", f"/bindings/{v}/e/{quote(self.exchange, safe='')}"
                                           f"/q/{quote(self.queue, safe='')}") or []
        return {"queue": {"type": queue.get("type"), "durable": queue.get("durable"),
                          "auto_delete": queue.get("auto_delete"),
                          "arguments": queue.get("arguments") or {}},
                "exchange": {"type": exchange.get("type"), "durable": exchange.get("durable"),
                             "arguments": exchange.get("arguments") or {}},
                "bindings": sorted((b["routing_key"], sorted((b.get("arguments") or {})
                                                             .items())) for b in bindings)}

    def _permissions(self, side):
        """user -> {configure, write, read} on the side's virtual host (None: none)."""
        v = self._v(side)
        out = {}
        for user in self.users:
            found = self._http(side, "GET", f"/permissions/{v}/{quote(user, safe='')}",
                               missing_ok=True)
            out[user] = None if found is None else {k: found[k] for k in
                                                    ("configure", "write", "read")}
        return out

    @staticmethod
    def _grants(permissions, kind):
        return {u: p[kind] for u, p in permissions.items() if p and p[kind] != NOTHING}

    def _connections(self, side):
        """Connections of the unit's users to the side's virtual host, as the
        broker tracks them now (not as the statistics last reported them)."""
        vhost, out = self._vhost(side), []
        for user in self.users:
            found = self._http(side, "GET", f"/connections/username/{quote(user, safe='')}",
                               missing_ok=True) or []
            out += [c["name"] for c in found if c.get("vhost") == vhost]
        return sorted(out)

    def _feeders(self, side):
        """Shovels delivering into the side's virtual host, federation upstreams in it."""
        vhost, out = self._vhost(side), []
        for shovel in self._http(side, "GET", "/parameters/shovel", missing_ok=True) or []:
            value = shovel.get("value") or {}
            uris = value.get("dest-uri") or []
            for uri in [uris] if isinstance(uris, str) else uris:
                path = urlsplit(uri).path
                if (unquote(path[1:]) if len(path) > 1 else "/") == vhost:
                    out.append(f"shovel:{shovel['vhost']}/{shovel['name']}")
        for upstream in self._http(side, "GET", f"/parameters/federation-upstream/"
                                                f"{self._v(side)}", missing_ok=True) or []:
            out.append(f"federation-upstream:{upstream['name']}")
        return sorted(set(out))

    def _set_permissions(self, side, user, configure, write, read):
        self._http(side, "PUT", f"/permissions/{self._v(side)}/{quote(user, safe='')}",
                   {"configure": configure, "write": write, "read": read})

    # -- actions -------------------------------------------------------------------
    def _stage_target(self):
        definitions = self._definitions("source")
        if definitions is None:
            raise ContractError("The source queue or exchange does not exist")
        v = self._v("target")
        self._http("target", "PUT", f"/vhosts/{v}", {})
        self._set_permissions("target", self.admin_user, ".*", ".*", ".*")
        for user in self.users:
            self._http("target", "DELETE", f"/permissions/{v}/{quote(user, safe='')}",
                       missing_ok=True)
        q, e = definitions["queue"], definitions["exchange"]
        self._http("target", "PUT", f"/exchanges/{v}/{quote(self.exchange, safe='')}",
                   {"type": e["type"], "durable": e["durable"], "arguments": e["arguments"]})
        self._http("target", "PUT", f"/queues/{v}/{quote(self.queue, safe='')}",
                   {"durable": q["durable"], "auto_delete": q["auto_delete"],
                    "arguments": {**q["arguments"], "x-queue-type": q["type"]}})
        for routing_key, arguments in definitions["bindings"]:
            self._http("target", "POST", f"/bindings/{v}/e/{quote(self.exchange, safe='')}"
                                         f"/q/{quote(self.queue, safe='')}",
                       {"routing_key": routing_key, "arguments": dict(arguments)})

    def _check_stage_target(self):
        source, target = self._definitions("source"), self._definitions("target")
        if target is None:
            return False, {"target_definitions": None}
        same = source is not None and _comparable(source) == _comparable(target)
        open_ = _open(self._permissions("target"))
        return same and not open_, {"same_definitions": same, "target_permissions": open_}

    def _pause_admission(self):
        permissions = self._permissions("source")
        for user in self.publishers:
            p = permissions[user]
            if p is not None:
                self._set_permissions("source", user, p["configure"], NOTHING, p["read"])

    def _check_pause_admission(self):
        writes = self._grants(self._permissions("source"), "write")
        writes = {u: w for u, w in writes.items() if u in self.publishers}
        return not writes, {"source_writes": writes}

    def _fence_source_writes(self):
        for user in self.users:
            if self._permissions("source")[user] is not None:
                self._set_permissions("source", user, NOTHING, NOTHING, NOTHING)
        for user in self.users:
            self._http("source", "DELETE", f"/connections/username/{quote(user, safe='')}",
                       missing_ok=True)
        # The broker drops a closing connection once its client confirms, or
        # after its closing timeout (30 s) when the client does not answer.
        deadline = time.monotonic() + self.close_wait_seconds
        while self._connections("source") and time.monotonic() < deadline:
            time.sleep(0.2)

    def _check_fence_source_writes(self):
        open_ = _open(self._permissions("source"))
        connections = self._connections("source")
        feeders = self._feeders("source")
        return not open_ and not connections and not feeders, {
            "source_permissions": open_, "source_connections": connections[:20],
            "feeders": feeders[:20]}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        present, detail = self._check_stage_target()
        connections = self._connections("target")
        return present and not connections, {**detail, "target_connections": connections}

    def _copy(self):
        import pika

        if self._count("target") is None:
            raise ContractError("The target queue is not staged")
        source = self._connect("source")
        target = self._connect("target")
        try:
            out = target.channel()
            out.queue_purge(self.queue)
            out.confirm_delivery()
            into = source.channel()
            into.basic_qos(prefetch_count=0)
            last = None
            while True:
                method, properties, body = into.basic_get(self.queue, auto_ack=False)
                if method is None:
                    break
                last = method.delivery_tag
                headers = {k: v for k, v in (properties.headers or {}).items()
                           if k not in _BROKER_HEADERS}
                copy = pika.BasicProperties(**{**{k: getattr(properties, k) for k in (
                    "content_type", "content_encoding", "delivery_mode", "priority",
                    "correlation_id", "reply_to", "expiration", "message_id", "timestamp",
                    "type", "user_id", "app_id")}, "headers": headers or None})
                out.basic_publish("", self.queue, body, copy, mandatory=True)
            if last is not None:  # every copy is confirmed: give the originals back
                into.basic_nack(delivery_tag=last, multiple=True, requeue=True)
        finally:
            for connection in (source, target):
                if connection.is_open:
                    connection.close()

    def _compare(self):
        source, target = self._count("source"), self._count("target")
        if source is None or target is None:
            return False, {"counts": [list(c) if c else None for c in (source, target)]}
        return source[0] == target[0] and target[1] == 0, {
            "ready": [source[0], target[0]], "target_consumers": target[1]}

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
        source, target = self._definitions("source"), self._definitions("target")
        equal = source is not None and target is not None and \
            _comparable(source) == _comparable(target)
        return same and equal, {**detail, "same_definitions": equal}

    def _activate_target(self):
        exchange = f"^{_regex(self.exchange)}$"
        queue = f"^{_regex(self.queue)}$"
        self._set_permissions("target", self.app_publisher, NOTHING, exchange, NOTHING)
        for user in self.consumers:
            self._set_permissions("target", user, NOTHING, NOTHING, queue)

    def _check_activate_target(self):
        target, source = self._permissions("target"), self._permissions("source")
        app = (target.get(self.app_publisher) or {}).get("write", NOTHING) != NOTHING
        others = {u: w for u, w in self._grants(target, "write").items()
                  if u != self.app_publisher}
        readers = all((target.get(u) or {}).get("read", NOTHING) != NOTHING
                      for u in self.consumers)
        open_ = _open(source)
        return app and not others and readers and not open_, {
            "target_permissions": target, "source_permissions": open_}

    def _commit_route(self):
        self.route.set(self.target_vhost)

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.target_vhost, {"route": current}


def _open(permissions):
    """The users that may still do anything: a permission entry that grants
    nothing (``^$`` everywhere) is as closed as none."""
    return {u: p for u, p in permissions.items()
            if p and any(p[k] != NOTHING for k in ("configure", "write", "read"))}


def _comparable(definitions):
    """Definitions as compared: the queue type is its own field, whether or not
    it was also given as the ``x-queue-type`` argument."""
    queue = definitions["queue"]
    return {**definitions, "queue": {**queue, "arguments": {
        k: v for k, v in queue["arguments"].items() if k != "x-queue-type"}}}


def _regex(name):
    import re

    return re.escape(name)
