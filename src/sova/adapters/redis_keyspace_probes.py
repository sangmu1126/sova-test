"""Independent probes for a Redis-protocol keyspace handoff (§16.3, §15.5, §28.2).

Made apart from the adapter that moved the keys:

* write denial needs two views to agree (ADR-0007): an attempt - a new
  connection as each registered writer, a real ``SET`` queued in a
  transaction that is always discarded (the server checks the permission
  when it queues the command) - and the server's own permission check
  (``ACL DRYRUN`` of a ``SET`` under each protected prefix) on the primary
  and on every registered replica, which keeps ACL users of its own and,
  promoted, would take the writer's writes. A refused login or command that
  no node would permit is a denial;
* each protected set is a key prefix of the unit, compared value by value
  with its expiry time;
* a business invariant is a registered reference: every hash under one prefix
  names, in one field, a key that exists under another prefix (a session's
  cart);
* the restore drill copies the target's keys into a scratch database of the
  target server and compares, then removes them;
* tenant isolation is the server's own check (``ACL DRYRUN``) that the
  application user may neither read nor write another tenant's prefix;
* transport encryption is this client's own connection: TLS, the server's
  certificate verified, its host name checked;
* placement comes from ``zone_of(side)``, the inventory's view, as read.

Each probe returns ``(passed, observation)``.
"""

import hashlib
import json
from uuid import uuid4

from sova.adapters.redis_keyspace_handoff import prefix as plain_prefix
from sova.domain.contracts import ContractError


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


class RedisKeyspaceProbes:
    def __init__(self, *, connect, connect_as, writers, app_user, sets, invariants=None,
                 scratch_db=15, other_prefix=None, zone_of=None, replicas=None):
        """``connect(side)`` is an administrative client, ``connect_as(side, user)``
        a client logged in as ``user``; ``sets`` maps each protected set to its
        key prefix; ``replicas(side)`` administrative clients on the side's
        registered replicas."""
        if not callable(connect) or not callable(connect_as):
            raise ContractError("Administrative and per-user connections required")
        self.replicas = replicas or (lambda side: ())
        if type(sets) is not dict or not sets:
            raise ContractError("A key prefix for each protected set required")
        self.connect, self.connect_as = connect, connect_as
        self.writers, self.app_user = tuple(writers), app_user
        self.sets = {name: plain_prefix(p) for name, p in sets.items()}
        self.invariants = dict(invariants or {})
        self.scratch_db, self.other_prefix, self.zone_of = scratch_db, other_prefix, zone_of

    # -- writes ------------------------------------------------------------------
    def _try_write(self, side, name):
        """Whether ``name`` may write the keyspace on ``side``, by trying.

        A ``SET`` queued in a transaction that is discarded leaves nothing; a
        user without transactions is tried with a ``SET`` of a key of its own
        that expires after a second (left only if the write was allowed, which
        is the failure being measured)."""
        import redis

        key = next(iter(self.sets.values())) + "sova-probe-" + uuid4().hex[:12]
        client = self.connect_as(side, name)
        try:
            try:  # the first command logs in: no other command is assumed allowed
                client.execute_command("MULTI")
            except redis.exceptions.AuthenticationError:
                return False, "login_refused"
            except redis.exceptions.NoPermissionError:
                client.execute_command("SET", key, "1", "NX", "PX", "1000")
                return True, "write_accepted"
            try:
                client.execute_command("SET", key, "1")
                return True, "write_accepted"
            finally:
                client.execute_command("DISCARD")
        except redis.exceptions.NoPermissionError:
            return False, "write_refused"
        finally:
            client.close()

    def _permitted(self, side, name):
        """The nodes whose own ACL check lets ``name`` SET a key of a protected set."""
        import redis

        found = []
        clients = [("primary", self.connect(side))] + [
            (f"replica-{i}", c) for i, c in enumerate(self.replicas(side))]
        for node, client in clients:
            for p in self.sets.values():
                try:
                    answer = _text(client.execute_command("ACL", "DRYRUN", name, "SET",
                                                          p + "sova-dryrun", "1"))
                except redis.exceptions.ResponseError as error:
                    if "not found" in str(error).lower():
                        break  # no such user on this node: it cannot write here
                    raise
                if answer == "OK":
                    found.append(node)
                    break
        return found

    def _denied(self, side, name):
        wrote, attempt = self._try_write(side, name)
        permitted = self._permitted(side, name)
        denied = not wrote and attempt in ("login_refused", "write_refused") and not permitted
        return denied, {"attempt": attempt, "permitted_on": permitted}

    def write_denied(self, side):
        outcomes = {w: self._denied(side, w) for w in self.writers}
        return all(d for d, _ in outcomes.values()), {
            "probe": "write_denied", "side": side,
            "outcomes": {w: detail for w, (_, detail) in outcomes.items()}}

    def single_writer(self):
        wrote, target = self._try_write("target", self.app_user)
        denied, source = self.write_denied("source")
        return wrote and denied, {"probe": "single_writer", "target": target,
                                  "source": source["outcomes"]}

    # -- content -----------------------------------------------------------------
    @staticmethod
    def _read(client, key):
        kind = _text(client.type(key))
        value = {"string": lambda: client.get(key),
                 "list": lambda: client.lrange(key, 0, -1),
                 "set": lambda: sorted(client.smembers(key)),
                 "zset": lambda: client.zrange(key, 0, -1, withscores=True),
                 "hash": lambda: sorted(client.hgetall(key).items()),
                 "stream": lambda: [client.xrange(key), client.xinfo_groups(key)],
                 }.get(kind, lambda: client.dump(key))()
        return kind, value

    def _digest(self, client, prefix):
        h, count = hashlib.sha256(), 0
        for key in sorted(client.scan_iter(match=prefix + "*", count=500)):
            kind, value = self._read(client, key)
            if kind == "none":
                continue
            h.update(repr((key, kind, value,
                           client.execute_command("PEXPIRETIME", key))).encode())
            count += 1
        return count, h.hexdigest()

    def content_equal(self, name):
        if name not in self.sets:
            raise ContractError("Unregistered protected set")
        source = self._digest(self.connect("source"), self.sets[name])
        target = self._digest(self.connect("target"), self.sets[name])
        return source == target and source[0] > 0, {
            "probe": "content_equal", "set": name, "prefix": self.sets[name],
            "source": {"keys": source[0], "digest": source[1]},
            "target": {"keys": target[0], "digest": target[1]}}

    def invariant(self, invariant_id):
        spec = self.invariants.get(invariant_id)
        if spec is None:
            return False, {"probe": "invariant", "invariant": invariant_id,
                           "reason": "no probe registered for this invariant"}
        client = self.connect("target")
        origin, field, target = (plain_prefix(spec["from"]), spec["field"],
                                 plain_prefix(spec["to"]))
        checked, missing = 0, []
        for key in client.scan_iter(match=origin + "*", count=500):
            if _text(client.type(key)) != "hash":
                continue
            named = client.hget(key, field)
            checked += 1
            if named is None or not client.exists(target + _text(named)):
                missing.append(_text(key))
        return checked > 0 and not missing, {"probe": "invariant", "invariant": invariant_id,
                                             "checked": checked, "missing": missing[:20]}

    # -- isolation, transport, placement ---------------------------------------------------
    def cross_tenant_denied(self, other=None):
        other = other or self.other_prefix
        if not other:
            return False, {"probe": "cross_tenant_denied", "reason": "no other tenant registered"}
        client = self.connect("target")
        answers = {command: _text(client.execute_command(
            "ACL", "DRYRUN", self.app_user, command, plain_prefix(other) + "x",
            *(("1",) if command == "SET" else ()))) for command in ("GET", "SET")}
        return all(a != "OK" for a in answers.values()), {
            "probe": "cross_tenant_denied", "other_prefix": other,
            "allowed": {c: a == "OK" for c, a in answers.items()}}

    def transport_encrypted(self, side):
        kwargs = self.connect(side).connection_pool.connection_kwargs
        tls = bool(kwargs.get("ssl")) or "ssl_cert_reqs" in kwargs
        verified = tls and kwargs.get("ssl_cert_reqs") in ("required", 2) and bool(
            kwargs.get("ssl_check_hostname"))
        return verified, {"probe": "transport_encrypted", "side": side, "tls": tls,
                          "client_verification": "verify-identity" if verified else "none"}

    def zone(self, side, allowed):
        label = self.zone_of(side) if self.zone_of else None
        return label in allowed, {"probe": "zone", "side": side, "label": label,
                                  "allowed": sorted(allowed),
                                  "source": "inventory" if self.zone_of else "none"}

    # -- restore -----------------------------------------------------------------------
    def restore_drill(self, scratch=None):
        """Copy the target's keys into a scratch database of the target server,
        compare them there, and remove them."""
        db = self.scratch_db if scratch is None or type(scratch) is not int else scratch
        target = self.connect("target")
        pool = target.connection_pool  # the same server, TLS and login; another database
        scratch_client = type(target)(connection_pool=type(pool)(
            connection_class=pool.connection_class, **{**pool.connection_kwargs, "db": db}))
        if scratch_client.dbsize():
            return False, {"probe": "restore_drill", "reason": f"scratch database {db} is "
                                                              "not empty"}
        restored = {}
        try:
            for name, prefix in self.sets.items():
                for key in target.scan_iter(match=prefix + "*", count=500):
                    payload = target.dump(key)
                    if payload is None:
                        continue
                    expires = target.execute_command("PEXPIRETIME", key)
                    scratch_client.execute_command(
                        "RESTORE", key, max(expires, 0), payload, "REPLACE",
                        *(("ABSTTL",) if expires > 0 else ()))
                restored[name] = (self._digest(target, prefix),
                                  self._digest(scratch_client, prefix))
        finally:
            scratch_client.flushdb()
            scratch_client.close()
        equal = all(a == b and a[0] > 0 for a, b in restored.values())
        return equal, {"probe": "restore_drill", "scratch_db": db, "sets": {
            name: {"keys": a[0], "equal": a == b} for name, (a, b) in restored.items()},
            "digest": hashlib.sha256(json.dumps(sorted(
                (n, a[1]) for n, (a, _) in restored.items())).encode()).hexdigest()}
