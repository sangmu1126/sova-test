"""Independent probes for a RabbitMQ queue handoff (§16.3, §15.5, §28.2).

A message cannot be read without changing it (it comes back marked
redelivered; a quorum queue counts the delivery), so these probes measure
only what can be seen without reading: who may publish, the queue's
definitions and how many messages it holds.

* write denial needs two views to agree: an attempt (each registered
  publisher logs in with its own credentials and publishes inside an AMQP
  transaction that is rolled back) and the broker's permission record for
  that user on the virtual host. A refused login or a refused publish
  (``ACCESS_REFUSED``) with no write permission on the exchange is a denial;
  any other failure decides nothing and counts against;
* content is the queue's, exchange's and binding's definitions and the ready
  message count, read on both sides;
* the restore drill restores the target's definitions into a scratch virtual
  host and compares them, then removes it. Messages are in-flight work, not
  part of a queue's restore: the drill says so instead of reading them;
* tenant isolation is an attempt: the application user logs in to another
  tenant's virtual host and must be refused;
* transport encryption is this client's own connection: TLS with the broker's
  certificate and host name verified;
* placement comes from ``zone_of(side)``, the inventory's view, as read.

Each probe returns ``(passed, observation)``.
"""

import re
import ssl
from urllib.parse import quote
from uuid import uuid4

from sova.domain.contracts import ContractError


class RabbitMqQueueProbes:
    def __init__(self, *, broker, connect_as, vhosts, queue, exchange, routing_key, publishers,
                 app_publisher, invariants=None, other_vhost=None, zone_of=None, timeout=10.0):
        """``broker(side)`` is the adapter's administrative access (management API,
        AMQP parameters, credentials); ``connect_as(side, user, vhost)`` gives
        AMQP parameters for ``user``'s own login."""
        if not callable(broker) or not callable(connect_as):
            raise ContractError("Administrative and per-user access required")
        if type(vhosts) is not dict or set(vhosts) != {"source", "target"}:
            raise ContractError("The virtual host of each side required")
        self.broker, self.connect_as, self.vhosts = broker, connect_as, vhosts
        self.queue, self.exchange, self.routing_key = queue, exchange, routing_key
        self.publishers, self.app_publisher = tuple(publishers), app_publisher
        self.other_vhost, self.zone_of, self.timeout = other_vhost, zone_of, timeout
        self.invariants = dict(invariants or {})

    # -- access ------------------------------------------------------------------
    def _http(self, side, method, path, body=None, *, missing_ok=False):
        import httpx

        b = self.broker(side)
        response = httpx.request(method, b["http"].rstrip("/") + "/api" + path, json=body,
                                 auth=b["auth"], timeout=self.timeout)
        if response.status_code == 404 and missing_ok:
            return None
        response.raise_for_status()
        return response.json() if response.content else None

    def _try_publish(self, side, user, vhost):
        import pika

        try:
            connection = pika.BlockingConnection(self.connect_as(side, user, vhost))
        except (pika.exceptions.ProbableAccessDeniedError,
                pika.exceptions.ProbableAuthenticationError):
            return False, "login_refused"
        except pika.exceptions.ConnectionClosedByBroker as error:
            return False, "login_refused" if error.reply_code in (403, 530) else \
                f"undecided:{error.reply_code}"
        except pika.exceptions.AMQPConnectionError as error:
            return False, f"undecided:{type(error).__name__}"
        try:
            channel = connection.channel()
            channel.tx_select()
            channel.basic_publish(self.exchange, self.routing_key, b"sova-probe")
            channel.tx_rollback()  # checked by the broker; nothing stays
            return True, "write_accepted"
        except pika.exceptions.ChannelClosedByBroker as error:
            return False, "write_refused" if error.reply_code == 403 else \
                f"undecided:{error.reply_code}"
        finally:
            if connection.is_open:
                connection.close()

    def _permitted(self, side, user):
        """Whether the broker's permission record lets ``user`` write the exchange."""
        vhost = quote(self.vhosts[side], safe="")
        found = self._http(side, "GET", f"/permissions/{vhost}/{quote(user, safe='')}",
                           missing_ok=True)
        if not found:
            return False, None
        pattern = found.get("write") or ""
        return pattern != "" and re.search(pattern, self.exchange) is not None, pattern

    def _denied(self, side, user):
        wrote, attempt = self._try_publish(side, user, self.vhosts[side])
        may, pattern = self._permitted(side, user)
        denied = not wrote and attempt in ("write_refused", "login_refused") and not may
        return denied, {"attempt": attempt, "write_permission": pattern}

    def write_denied(self, side):
        outcomes = {u: self._denied(side, u) for u in self.publishers}
        return all(d for d, _ in outcomes.values()), {
            "probe": "write_denied", "side": side, "vhost": self.vhosts[side],
            "outcomes": {u: detail for u, (_, detail) in outcomes.items()}}

    def single_writer(self):
        wrote, target = self._try_publish("target", self.app_publisher, self.vhosts["target"])
        denied, source = self.write_denied("source")
        return wrote and denied, {"probe": "single_writer", "target": target,
                                  "source": source["outcomes"]}

    # -- content -----------------------------------------------------------------
    def _definitions(self, side, vhost=None):
        v = quote(vhost or self.vhosts[side], safe="")
        queue = self._http(side, "GET", f"/queues/{v}/{quote(self.queue, safe='')}",
                           missing_ok=True)
        exchange = self._http(side, "GET", f"/exchanges/{v}/{quote(self.exchange, safe='')}",
                              missing_ok=True)
        if queue is None or exchange is None:
            return None
        bindings = self._http(side, "GET", f"/bindings/{v}/e/{quote(self.exchange, safe='')}"
                                           f"/q/{quote(self.queue, safe='')}") or []
        return {"queue": {k: queue.get(k) for k in ("type", "durable", "auto_delete")}
                | {"arguments": queue.get("arguments") or {}},
                "exchange": {k: exchange.get(k) for k in ("type", "durable")}
                | {"arguments": exchange.get("arguments") or {}},
                "bindings": sorted((b["routing_key"], sorted((b.get("arguments") or {})
                                                             .items())) for b in bindings)}

    def _ready(self, side):
        import pika

        connection = pika.BlockingConnection(self.broker(side)["amqp"](self.vhosts[side]))
        try:
            return connection.channel().queue_declare(self.queue,
                                                      passive=True).method.message_count
        except pika.exceptions.ChannelClosedByBroker:
            return None
        finally:
            if connection.is_open:
                connection.close()

    def content_equal(self, name):
        source = (self._definitions("source"), self._ready("source"))
        target = (self._definitions("target"), self._ready("target"))
        return source == target and source[0] is not None, {
            "probe": "content_equal", "set": name, "method": "definitions_and_count",
            "source": {"ready": source[1]}, "target": {"ready": target[1]},
            "definitions_equal": source[0] == target[0]}

    def invariant(self, invariant_id):
        """A registered invariant over what can be seen without reading a message:
        ``same_count`` (no waiting work lost or doubled by the handoff) or
        ``max_ready`` (the target holds at most ``value`` waiting messages)."""
        spec = self.invariants.get(invariant_id)
        if spec is None:
            return False, {"probe": "invariant", "invariant": invariant_id,
                           "reason": "a queue's messages cannot be read without changing "
                                     "them; only a registered count invariant is measured"}
        source, target = self._ready("source"), self._ready("target")
        if spec.get("type") == "same_count":
            holds = source is not None and source == target
        elif spec.get("type") == "max_ready":
            holds = target is not None and target <= int(spec["value"])
        else:
            raise ContractError("Unknown invariant type")
        return holds, {"probe": "invariant", "invariant": invariant_id, "type": spec["type"],
                       "source_ready": source, "target_ready": target}

    # -- isolation, transport, placement ---------------------------------------------------
    def cross_tenant_denied(self, other=None):
        vhost = other or self.other_vhost
        if not vhost:
            return False, {"probe": "cross_tenant_denied", "reason": "no other tenant registered"}
        wrote, attempt = self._try_publish("target", self.app_publisher, vhost)
        return not wrote and attempt in ("login_refused", "write_refused"), {
            "probe": "cross_tenant_denied", "other_vhost": vhost, "attempt": attempt}

    def transport_encrypted(self, side):
        options = getattr(self.broker(side)["amqp"](self.vhosts[side]), "ssl_options", None)
        context = getattr(options, "context", None)
        verified = bool(context and context.verify_mode == ssl.CERT_REQUIRED
                        and context.check_hostname)
        return verified, {"probe": "transport_encrypted", "side": side, "tls": bool(options),
                          "client_verification": "verify-identity" if verified else "none"}

    def zone(self, side, allowed):
        label = self.zone_of(side) if self.zone_of else None
        return label in allowed, {"probe": "zone", "side": side, "label": label,
                                  "allowed": sorted(allowed),
                                  "source": "inventory" if self.zone_of else "none"}

    # -- restore -----------------------------------------------------------------------
    def restore_drill(self, scratch=None):
        """Restore the target's definitions into a scratch virtual host, compare."""
        wanted = self._definitions("target")
        if wanted is None:
            return False, {"probe": "restore_drill", "reason": "target definitions missing"}
        vhost = f"{scratch or 'sova-restore'}-{uuid4().hex[:8]}"
        v, admin = quote(vhost, safe=""), self.broker("target")["auth"][0]
        try:
            self._http("target", "PUT", f"/vhosts/{v}", {})
            self._http("target", "PUT", f"/permissions/{v}/{quote(admin, safe='')}",
                       {"configure": ".*", "write": ".*", "read": ".*"})
            self._http("target", "PUT", f"/exchanges/{v}/{quote(self.exchange, safe='')}",
                       {**wanted["exchange"], "auto_delete": False})
            self._http("target", "PUT", f"/queues/{v}/{quote(self.queue, safe='')}",
                       {"durable": wanted["queue"]["durable"],
                        "auto_delete": wanted["queue"]["auto_delete"],
                        "arguments": wanted["queue"]["arguments"]})
            for key, arguments in wanted["bindings"]:
                self._http("target", "POST", f"/bindings/{v}/e/{quote(self.exchange, safe='')}"
                                             f"/q/{quote(self.queue, safe='')}",
                           {"routing_key": key, "arguments": dict(arguments)})
            restored = self._definitions("target", vhost)
        finally:
            self._http("target", "DELETE", f"/vhosts/{v}", missing_ok=True)
        return restored == wanted, {"probe": "restore_drill", "scratch_vhost": vhost,
                                    "restored": "definitions",
                                    "messages": "in-flight work, not part of a restore"}
