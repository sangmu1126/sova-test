"""Redis-protocol keyspace handoff world for the provider-neutral conformance kit.

Observed independently of the adapter: content is read key by key with the
world's own client and canonical form, with each key's expiry time; access is
the server's raw ACL list for the unit's users; a write attempt is a real
command as the user inside a transaction that is always discarded (the
server checks permissions when the command is queued); the write path the
fence does not own is a further ACL user with write access to the keyspace.
"""

import hashlib
import json

from redis_business import PASSWORD, as_user

from sova.adapters.redis_keyspace_handoff import RedisKeyspaceHandoffAdapter


class Route:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class RedisWorld:
    def __init__(self, business):
        self.b, self.prefix, self.users = business, business["prefix"], business["users"]
        self.writers = {"app": self.users["app"], "other": self.users["batch"]}
        self._route = Route("source")
        self.pending = None

    def _side(self, side):
        return "source" if side == "source" else "target"

    def _adapter(self, source, target, routes):
        clients = {"source": self.b["admin"][source], "target": self.b["admin"][target]}
        return RedisKeyspaceHandoffAdapter(
            "redis-keyspace-handoff", connect=lambda side: clients[side],
            prefixes=(self.prefix + "*",), writers=(self.users["app"], self.users["batch"]),
            app_user=self.users["app"], admin_user="sova-admin", route=self._route,
            routes=routes)

    def adapter(self):
        return self._adapter("source", "target", ("source", "target"))

    def reverse_adapter(self):
        return self._adapter("target", "source", ("target", "source"))

    # -- observation -------------------------------------------------------------
    def content(self, side):
        client = self.b["admin"][self._side(side)]
        keys = sorted(client.scan_iter(match=self.prefix + "*", count=500))
        if not keys:
            return None
        h = hashlib.sha256()
        for key in keys:
            kind = client.type(key).decode()
            read = {"string": lambda k: client.get(k),
                    "list": lambda k: client.lrange(k, 0, -1),
                    "set": lambda k: sorted(client.smembers(k)),
                    "zset": lambda k: client.zrange(k, 0, -1, withscores=True),
                    "hash": lambda k: sorted(client.hgetall(k).items()),
                    "stream": lambda k: [client.xrange(k), client.xinfo_groups(k)]}[kind]
            h.update(repr((key, kind, read(key),
                           client.execute_command("PEXPIRETIME", key))).encode())
        return h.hexdigest()

    def _access(self, side):
        lines = [line.decode() for line in
                 self.b["admin"][self._side(side)].execute_command("ACL", "LIST")]
        names = set(self.users.values()) | ({self.pending} if self.pending else set())
        return sorted(line for line in lines if line.split(" ")[1] in names)

    def state(self):
        return (self.content("source"), self.content("target"), self._access("source"),
                self._access("target"), self._route.get())

    # -- the business and writers -----------------------------------------------------
    def can_write(self, side, writer):
        redis = self.b["redis"]
        client = as_user(self.b, self._side(side), self.writers[writer])
        try:
            try:  # the first command logs in: no other command is assumed allowed
                client.execute_command("MULTI")
            except redis.exceptions.AuthenticationError:
                return False  # the user is off or does not exist on that server
            except redis.exceptions.NoPermissionError:  # no transactions: try a write that
                client.set(self.prefix + "sova-probe-short", "1", nx=True, px=1000)  # expires
                return True
            try:
                client.execute_command("SET", self.prefix + "sova-probe", "1")
                return True
            finally:
                client.execute_command("DISCARD")  # the write is checked; nothing stays
        except redis.exceptions.NoPermissionError:
            return False
        finally:
            client.close()

    def business_write(self, count):
        client = as_user(self.b, "source", self.users["app"])
        for i in range(count):
            key = f"{self.prefix}session:late-{i}"
            client.hset(key, mapping={"user": f"late-{i}"})
            client.expire(key, 7200)
            client.incr(f"{self.prefix}orders:issued")
        client.close()

    def business_delete(self, count):
        client = as_user(self.b, "source", self.users["app"])
        client.delete(*[f"{self.prefix}session:{i}" for i in range(1, count + 1)])
        client.close()

    # -- changes outside the plan ----------------------------------------------------------
    def reopen_source(self):
        """The application's access restored as it was before the handoff."""
        self.b["admin"]["source"].execute_command(
            "ACL", "SETUSER", self.users["app"], "on", f"~{self.prefix}*")

    def tamper_target(self):
        self.b["admin"]["target"].set(self.prefix + "forged", json.dumps({"forged": True}))

    def move_route(self, side):
        self._route.set(self._side(side))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return self._side(side)

    def open_pending_write(self):
        """A further user, outside the registered writers, that may write the keys."""
        self.pending = f"maintenance-{self.users['app'].rsplit('-', 1)[1]}"
        self.b["admin"]["source"].execute_command(
            "ACL", "SETUSER", self.pending, "on", f">{PASSWORD}", f"~{self.prefix}*", "+@all")

    def resolve_pending_write(self):
        self.b["admin"]["source"].execute_command("ACL", "DELUSER", self.pending)
