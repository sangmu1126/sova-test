"""MongoDB database handoff world for the provider-neutral conformance kit.

Observed independently of the adapter: content is every document in its
canonical extended JSON, in ``_id`` order (the adapter compares raw BSON);
access is each writer's roles and authentication restrictions as the server
reports them; a write attempt is a real ``update`` as the user that matches
no document (the server checks the privilege, nothing changes); the write
path the fence does not own is a further user with ``readWrite``.
"""

import hashlib

from bson import json_util
from mongodb_business import PASSWORD, as_user

from sova.adapters.mongodb_handoff import MongoDatabaseHandoffAdapter

COLLECTIONS = ("orders", "idempotency_results")


class Route:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class MongoWorld:
    def __init__(self, business):
        self.b, self.names, self.users = business, business["names"], business["users"]
        self.writers = {"app": self.users["app"], "other": self.users["batch"]}
        self._route = Route(self.names["src"])
        self.pending = None

    def _side(self, side):
        return "source" if side == "source" else "target"

    def _db(self, side):
        return self.names["src" if side == "source" else "tgt"]

    def _adapter(self, source, target):
        clients = {"source": self.b["admin"][source], "target": self.b["admin"][target]}
        names = {"source": self._db(source), "target": self._db(target)}
        return MongoDatabaseHandoffAdapter(
            "mongodb-handoff", connect=lambda side: clients[side], source_db=names["source"],
            target_db=names["target"], collections=COLLECTIONS, writers=self.b["writers"],
            app_user=f"{self.users['app']}@admin", admin_user="sova-admin@admin",
            route=self._route)

    def adapter(self):
        return self._adapter("source", "target")

    def reverse_adapter(self):
        return self._adapter("target", "source")

    # -- observation -------------------------------------------------------------
    def content(self, side):
        db = self.b["admin"][self._side(side)][self._db(side)]
        if not set(COLLECTIONS) <= set(db.list_collection_names()):
            return None
        h = hashlib.sha256()
        for collection in COLLECTIONS:
            for document in db[collection].find({}, sort=[("_id", 1)]):
                h.update(json_util.dumps(document, json_options=json_util.CANONICAL_JSON_OPTIONS)
                         .encode())
            h.update(repr(sorted((ix["name"], sorted(ix["key"].items()), ix.get("unique"))
                                 for ix in db[collection].list_indexes())).encode())
        return h.hexdigest()

    def _access(self, side):
        client = self.b["admin"][self._side(side)]
        out = []
        for user in sorted(self.users.values()) + ([self.pending] if self.pending else []):
            found = client.admin.command("usersInfo", user,
                                         showAuthenticationRestrictions=True)["users"]
            out.append(repr([(u["user"], sorted((r["role"], r["db"]) for r in u["roles"]),
                              u.get("authenticationRestrictions")) for u in found]))
        return out

    def state(self):
        return (self.content("source"), self.content("target"), self._access("source"),
                self._access("target"), self._route.get())

    # -- the business and writers -----------------------------------------------------
    def can_write(self, side, writer):
        errors = self.b["pymongo"].errors
        client = as_user(self.b, self._side(side), self.writers[writer])
        try:
            client[self._db(side)].orders.update_one({"_id": "__sova_probe_none__"},
                                                     {"$set": {"probe": True}})
            return True  # allowed, and it matched nothing
        except errors.OperationFailure as error:
            if error.code in (13, 18):  # unauthorized, or the login refused
                return False
            raise
        finally:
            client.close()

    def business_write(self, count):
        client = as_user(self.b, "source", self.users["app"])
        db = client[self._db("source")]
        start = 1000 + (db.orders.count_documents({}) or 0)
        for i in range(start, start + count):
            db.orders.insert_one({"_id": i, "customer": f"customer-{i % 7}",
                                  "idempotency_key": f"key-{i}", "amount": i})
        client.close()

    def business_delete(self, count):
        client = as_user(self.b, "source", self.users["app"])
        db = client[self._db("source")]
        for document in list(db.orders.find({}, sort=[("_id", 1)], limit=count)):
            db.orders.delete_one({"_id": document["_id"]})
        client.close()

    # -- changes outside the plan ----------------------------------------------------------
    def reopen_source(self):
        """The application's user restored as it was before the handoff."""
        admin = self.b["admin"]["source"].admin
        admin.command("grantRolesToUser", self.users["app"],
                      roles=[{"role": "readWrite", "db": self._db("source")}])
        admin.command("updateUser", self.users["app"], authenticationRestrictions=[])

    def tamper_target(self):
        self.b["admin"]["target"][self._db("target")].orders.update_one(
            {"_id": 1}, {"$inc": {"amount": 1}})

    def move_route(self, side):
        self._route.set(self._db(side))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return self._db(side)

    def open_pending_write(self):
        """A further user, outside the registered writers, that may write the data."""
        self.pending = f"maintenance-{self.users['app'].rsplit('-', 1)[1]}"
        self.b["admin"]["source"].admin.command(
            "createUser", self.pending, pwd=PASSWORD,
            roles=[{"role": "readWrite", "db": self._db("source")}])

    def resolve_pending_write(self):
        self.b["admin"]["source"].admin.command("dropUser", self.pending)
