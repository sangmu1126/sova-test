"""Reference adapter: an application's keyspace on a Redis-protocol server
(Valkey, Redis) moved to another server (§16.1, §16.5).

The unit is the keys under the application's registered prefixes, in one
logical database; the writers are ACL users. The same actions and
postconditions as the other data adapters, read back from the servers:

* Pausing admission turns the application user ``off``: no new connection
  can authenticate as it. Connections already open keep working until the
  fence, as sessions do elsewhere.
* The fence turns every registered writer ``off``, takes away all its key
  access (``resetkeys``) and ends its connections (``CLIENT KILL USER``). It
  reads back as done only when, besides, no other ACL user that is ``on`` may
  write a key of the unit (``ACL DRYRUN``, the server's own permission check:
  the ``default`` user with full access is such a user), and the source is a
  primary: a replica takes writes from its primary, outside any ACL.
* Staging copies each writer's ACL rules (password hashes, key patterns,
  commands) from the source to the target and leaves the user ``off``;
  activation turns only the application user ``on`` there.
* Copying is ``DUMP``/``RESTORE`` with the absolute expiry time, so a session
  that expires at 12:00 on the source expires at 12:00 on the target; keys of
  the unit the source no longer has are removed. ``start_sync`` copies while
  the source still serves and leaves a marker key outside the unit.
* Content is compared logically, per type (strings, lists, sets, sorted sets,
  hashes, streams with their consumer groups) with each key's expiry time:
  two servers' encodings of the same value may differ, its content may not.

Replicas: a Redis-protocol server keeps its ACL users per node; replication
carries data, not ACLs. A replica the fence did not reach would, promoted
by a failover, take the writer's writes again. So every ACL change goes to
the primary and to each registered replica (``replicas(side)``), and every
access postcondition holds on all of them. Besides, the primary must report
exactly the registered replicas, each following it (same replication ID):
an unregistered replica reads back UNKNOWN.

Key prefixes are plain (``board:`` or ``board:*``); a glob with classes or
escapes is refused rather than guessed at. Every action is idempotent; a
postcondition that does not hold reads back UNKNOWN.
"""

from datetime import UTC, datetime
import hashlib
import json
import re
import time

from sova.adapters.evidence_bounds import bounded
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
_USER = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")
_PREFIX = re.compile(r"[!-~]{1,200}")  # printable, no space; checked for globs below
_BATCH = 500


def user(value):
    if type(value) is not str or not _USER.fullmatch(value):
        raise ContractError("Plain ACL user name required")
    return value


def prefix(value):
    """``board:`` or ``board:*`` -> ``board:``; nothing else is a prefix."""
    plain = value[:-1] if type(value) is str and value.endswith("*") else value
    if type(plain) is not str or not _PREFIX.fullmatch(plain) or set(plain) & set("*?[]\\"):
        raise ContractError("A plain key prefix required")
    return plain


def acl_rules(line):
    """The rules of one ``ACL LIST`` line, a selector ``(...)`` kept whole."""
    tokens, depth, current = [], 0, ""
    for ch in line:
        if ch == " " and depth == 0:
            if current:
                tokens.append(current)
            current = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        current += ch
    if current:
        tokens.append(current)
    if len(tokens) < 2 or tokens[0] != "user":
        raise ContractError("Unexpected ACL LIST line")
    return tokens[1], tokens[2:]


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _value(v):
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return {"b": v.hex()}
    if isinstance(v, float):
        return {"f": repr(v)}
    if isinstance(v, (list, tuple)):
        return [_value(x) for x in v]
    if isinstance(v, dict):
        return sorted([_value(k), _value(x)] for k, x in v.items())
    return v


class RedisKeyspaceHandoffAdapter:
    def __init__(self, adapter_id, *, connect, prefixes, writers, app_user, admin_user, route,
                 routes, db=0, kill_wait_seconds=5.0, replicas=None):
        """``connect(side)`` gives an administrative client (redis-py, bytes
        responses) on that side's primary, ``db`` selected; ``replicas(side)``
        administrative clients on each of its registered replicas."""
        if not callable(connect) or not all(callable(getattr(route, n, None))
                                            for n in ("get", "set")):
            raise ContractError("Administrative connect and a route store required")
        if replicas is not None and not callable(replicas):
            raise ContractError("replicas(side) must give the side's replica clients")
        self.replicas = replicas or (lambda side: ())
        if type(prefixes) is not tuple or not prefixes:
            raise ContractError("Key prefixes required")
        if type(writers) is not tuple or app_user not in writers:
            raise ContractError("The application user must be a registered writer")
        if type(routes) is not tuple or len(routes) != 2 or routes[0] == routes[1]:
            raise ContractError("Distinct source and target route values required")
        self.adapter_id, self.connect, self.route, self.routes = (
            adapter_id, connect, route, routes)
        self.prefixes = tuple(prefix(p) for p in prefixes)
        self.writers = tuple(user(w) for w in writers)
        self.app_user, self.admin_user = user(app_user), user(admin_user)
        self.db, self.kill_wait_seconds = db, kill_wait_seconds
        self.marker = "__sova:sync:" + hashlib.sha256(json.dumps(
            [adapter_id, self.prefixes]).encode()).hexdigest()[:16]

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

    # -- access ----------------------------------------------------------------
    def _client(self, side):
        return self.connect(side)

    def _nodes(self, side):
        """The side's primary, then each registered replica: (name, client)."""
        return [("primary", self._client(side))] + [
            (f"replica-{i}", client) for i, client in enumerate(self.replicas(side))]

    def _all(self, side, *command):
        for _, client in self._nodes(side):
            client.execute_command(*command)

    def _acl(self, side, client=None):
        """user -> rules, from the server's own ACL LIST."""
        lines = (client or self._client(side)).execute_command("ACL", "LIST")
        return dict(acl_rules(_text(line)) for line in lines)

    @staticmethod
    def _replication(client):
        """The node's ``INFO replication`` (parsed by the client)."""
        return client.info("replication")

    def _topology(self, side):
        """Whether the primary reports exactly the registered replicas, each
        following it; (holds, detail)."""
        nodes = self._nodes(side)
        primary = self._replication(nodes[0][1])
        replid = primary.get("master_replid")
        followers = {name: self._replication(client) for name, client in nodes[1:]}
        reported = int(primary.get("connected_slaves", 0))
        following = {name: info.get("role") == "slave" and info.get("master_replid") == replid
                     and info.get("master_link_status") == "up"
                     for name, info in followers.items()}
        holds = primary.get("role") == "master" and reported == len(followers) and all(
            following.values())
        return holds, {"role": primary.get("role"), "replicas_reported": reported,
                       "replicas_registered": len(followers), "following": following}

    @staticmethod
    def _on(rules):
        return "on" in rules

    def _may_write(self, side, name, client=None):
        """Each prefix: whether ``name`` may SET a key under it (the server decides)."""
        client = client or self._client(side)
        out = {}
        for p in self.prefixes:
            answer = _text(client.execute_command("ACL", "DRYRUN", name, "SET",
                                                  p + "sova-dryrun", "1"))
            out[p] = answer == "OK"
        return out

    def _connected(self, side, names, client=None):
        listed = _text((client or self._client(side)).execute_command("CLIENT", "LIST"))
        found = []
        for line in listed.splitlines():
            fields = dict(f.split("=", 1) for f in line.split(" ") if "=" in f)
            if fields.get("user") in names:
                found.append({"id": fields.get("id"), "user": fields["user"],
                              "addr": fields.get("addr"), "flags": fields.get("flags")})
        return found

    def _writer_state(self, side, acl=None, client=None):
        acl = self._acl(side, client) if acl is None else acl
        state = {}
        for name in self.writers:
            rules = acl.get(name)
            state[name] = None if rules is None else {
                "on": self._on(rules), "may_write": self._may_write(side, name, client)}
        return state

    def _denied(self, state):
        return all(s is not None and not s["on"] and not any(s["may_write"].values())
                   for s in state.values())

    # -- content -----------------------------------------------------------------
    def _keys(self, side):
        client = self._client(side)
        found = set()
        for p in self.prefixes:
            for key in client.scan_iter(match=_glob(p) + "*", count=_BATCH):
                found.add(key)
        return sorted(found)

    def _digest(self, side):
        """(key count, digest) of the unit's logical content with expiry times."""
        client = self._client(side)
        h, count = hashlib.sha256(), 0
        for key in self._keys(side):
            kind = _text(client.type(key))
            if kind == "none":
                continue  # expired between the scan and now
            if kind == "string":
                value = client.get(key)
            elif kind == "list":
                value = client.lrange(key, 0, -1)
            elif kind == "set":
                value = sorted(client.smembers(key))
            elif kind == "zset":
                value = [[m, repr(s)] for m, s in client.zrange(key, 0, -1, withscores=True)]
            elif kind == "hash":
                value = client.hgetall(key)
            elif kind == "stream":
                entries = client.xrange(key, "-", "+")
                groups = [{k: g.get(k) for k in ("name", "last-delivered-id", "pending")}
                          for g in client.xinfo_groups(key)]
                value = [[e[0], e[1]] for e in entries] + [sorted(map(str, groups))]
            else:  # a module type: its serialization, which the same version shares
                value = client.dump(key)
            expires = client.execute_command("PEXPIRETIME", key)
            h.update(json.dumps([_value(key), kind, _value(value), expires],
                                separators=(",", ":")).encode() + b"\n")
            count += 1
        return count, h.hexdigest()

    # -- actions ---------------------------------------------------------------
    def _stage_target(self):
        source = self._acl("source")
        for name in self.writers:
            rules = source.get(name)
            if rules is None:
                raise ContractError(f"Writer {name} is not an ACL user on the source")
            kept = [r for r in rules if r not in ("on", "off")]
            self._all("target", "ACL", "SETUSER", name, "reset", *kept, "off")
            self._all("target", "CLIENT", "KILL", "USER", name)  # none may linger

    def _check_stage_target(self):
        return self._prepared("target")

    def _prepared(self, side):
        nodes, holds = {}, True
        for node, client in self._nodes(side):
            acl = self._acl(side, client)
            state = {name: None if acl.get(name) is None else {"on": self._on(acl[name])}
                     for name in self.writers}
            connected = self._connected(side, set(self.writers), client)
            holds &= all(s is not None and not s["on"] for s in state.values()) and \
                not connected
            nodes[node] = {"writers": state, "writer_connections": connected[:20]}
        complete, topology = self._topology(side)
        return holds and complete, {"nodes": nodes, "topology": topology}

    def _pause_admission(self):
        self._all("source", "ACL", "SETUSER", self.app_user, "off")

    def _check_pause_admission(self):
        off = {}
        for node, client in self._nodes("source"):
            rules = self._acl("source", client).get(self.app_user)
            off[node] = rules is not None and not self._on(rules)
        return all(off.values()), {"application_user_off": off}

    def _fence_source_writes(self):
        for name in self.writers:
            self._all("source", "ACL", "SETUSER", name, "off", "resetkeys")
        for name in self.writers:
            self._all("source", "CLIENT", "KILL", "USER", name)
        deadline = time.monotonic() + self.kill_wait_seconds
        while time.monotonic() < deadline and any(
                self._connected("source", set(self.writers), client)
                for _, client in self._nodes("source")):
            time.sleep(0.05)

    def _check_fence_source_writes(self):
        return self._fenced("source")

    def _fenced(self, side):
        """Every node of the side: the writers off with no key access, no other
        user that may write the unit, no writer connection; and the side's
        replicas exactly the registered ones."""
        nodes, holds = {}, True
        for node, client in self._nodes(side):
            acl = self._acl(side, client)
            writers = self._writer_state(side, acl, client)
            others = {}
            for name, rules in acl.items():
                if name in self.writers or name == self.admin_user or not self._on(rules):
                    continue
                may = self._may_write(side, name, client)
                if any(may.values()):
                    others[name] = may
            connected = self._connected(side, set(self.writers), client)
            holds &= self._denied(writers) and not others and not connected
            nodes[node] = {"writers": writers, "other_users_that_may_write": others,
                           "writer_connections": connected[:20]}
        complete, topology = self._topology(side)
        return holds and complete, {"nodes": nodes, "topology": topology}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        return self._fenced("target")

    def _copy(self):
        source, target = self._client("source"), self._client("target")
        keys = self._keys("source")
        present = set(self._keys("target"))
        stale = sorted(present - set(keys))
        for i in range(0, len(stale), _BATCH):
            target.delete(*stale[i:i + _BATCH])
        for i in range(0, len(keys), _BATCH):
            batch = keys[i:i + _BATCH]
            read = source.pipeline(transaction=False)
            for key in batch:
                read.dump(key)
                read.execute_command("PEXPIRETIME", key)
            answers = read.execute()
            write = target.pipeline(transaction=False)
            for key, payload, expires in zip(batch, answers[0::2], answers[1::2]):
                if payload is None:
                    write.delete(key)  # expired or removed since the scan
                    continue
                write.execute_command("RESTORE", key, max(expires, 0), payload, "REPLACE",
                                      *(("ABSTTL",) if expires > 0 else ()))
            write.execute()
        return len(keys)

    def _start_sync(self):
        copied = self._copy()
        self._client("target").set(self.marker, json.dumps(
            {"at": datetime.now(UTC).isoformat(), "keys": copied}))

    def _check_start_sync(self):
        marked = self._client("target").get(self.marker) is not None
        return marked, {"synced": marked}

    def _copy_content(self):
        self._copy()

    def _check_copy_content(self):
        return self._compare()

    def _compare(self):
        source, target = self._digest("source"), self._digest("target")
        return source == target, {"source": {"keys": source[0], "digest": source[1]},
                                  "target": {"keys": target[0], "digest": target[1]}}

    def _apply_final_changes(self):
        self._copy()

    def _check_apply_final_changes(self):
        return self._compare()

    def _verify_final_state(self):
        return None  # measurement only; the readback is the comparison

    def _check_verify_final_state(self):
        return self._compare()

    def _activate_target(self):
        self._all("target", "ACL", "SETUSER", self.app_user, "on")

    def _check_activate_target(self):
        """On every node of the target (a promoted replica serves the
        application too): only the application user enabled."""
        enabled, others = {}, {}
        for node, client in self._nodes("target"):
            acl = self._acl("target", client)
            app = acl.get(self.app_user)
            enabled[node] = app is not None and self._on(app) and all(
                self._may_write("target", self.app_user, client).values())
            others[node] = {name: self._on(acl[name]) if name in acl else None
                            for name in self.writers if name != self.app_user}
        complete, topology = self._topology("target")
        fenced, source = self._fenced("source")
        holds = all(enabled.values()) and not any(
            v for found in others.values() for v in found.values()) and complete and fenced
        return holds, {"application_user_enabled": enabled, "other_writers_on": others,
                       "topology": topology, "source": source}

    def _commit_route(self):
        self.route.set(self.routes[1])

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.routes[1], {"route": current}


def _glob(plain):
    """A plain prefix as a SCAN MATCH pattern (glob characters escaped)."""
    return re.sub(r"([*?\[\]\\])", r"\\\1", plain)
