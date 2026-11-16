"""Independent probes for a MongoDB database handoff (§16.3, §15.5, §28.2).

Made apart from the adapter that moved the documents, by other means:

* write denial needs two views to agree: an attempt (each registered writer
  logs in with its own credentials and sends an ``update`` that matches no
  document - the server checks the privilege, nothing changes) and the
  server's own account of the writer's privileges (``connectionStatus`` as
  that user). A refused login or a refused write with no write privilege is a
  denial; any other failure decides nothing and counts against;
* each protected set is a collection, compared as canonical extended JSON in
  ``_id`` order (the adapter compares raw BSON) with its indexes;
* a business invariant is a registered check on the target: no two documents
  share a registered key, or a registered aggregation returns nothing;
* the restore drill copies the target's collections into a scratch database
  of the target deployment, compares them, and drops it;
* tenant isolation is an attempt: the application user reads another
  tenant's database and must be refused;
* transport encryption is this client's own connection: TLS with the
  server's certificate and host name verified;
* placement comes from ``zone_of(side)``, the inventory's view, as read.

Each probe returns ``(passed, observation)``.
"""

import hashlib

from sova.adapters.mongodb_handoff import may_write
from sova.domain.contracts import ContractError


class MongoDatabaseProbes:
    def __init__(self, *, connect, connect_as, databases, collections, writers, app_user,
                 invariants=None, other_db=None, zone_of=None):
        """``connect(side)`` is an administrative client, ``connect_as(side, user)`` a
        client logging in as ``user`` (``user@authdb``); ``databases`` maps side to
        the unit's database."""
        if not callable(connect) or not callable(connect_as):
            raise ContractError("Administrative and per-user clients required")
        self.connect, self.connect_as, self.databases = connect, connect_as, databases
        self.collections, self.writers = tuple(collections), tuple(writers)
        self.app_user, self.invariants = app_user, dict(invariants or {})
        self.other_db, self.zone_of = other_db, zone_of

    # -- writes ------------------------------------------------------------------
    def _attempt(self, side, writer, database):
        import pymongo

        client = self.connect_as(side, writer)
        try:
            client[database][self.collections[0]].update_one(
                {"_id": "__sova_probe_none__"}, {"$set": {"probe": True}})
            return True, "write_accepted"
        except pymongo.errors.OperationFailure as error:
            if error.code == 18:
                return False, "login_refused"
            if error.code == 13:
                return False, "write_refused"
            return False, f"undecided:{error.code}"
        except pymongo.errors.PyMongoError as error:
            return False, f"undecided:{type(error).__name__}"
        finally:
            client.close()

    def _privileges(self, side, writer):
        """The writer's privileges as the server reports them to the writer itself."""
        import pymongo

        client = self.connect_as(side, writer)
        try:
            status = client.admin.command("connectionStatus", showPrivileges=True)
            return status.get("authInfo", {}).get("authenticatedUserPrivileges") or []
        except pymongo.errors.OperationFailure as error:
            if error.code == 18:
                return []  # it cannot log in at all
            raise
        finally:
            client.close()

    def _denied(self, side, writer):
        database = self.databases[side]
        wrote, attempt = self._attempt(side, writer, database)
        may = may_write(self._privileges(side, writer), database)
        return (not wrote and attempt in ("write_refused", "login_refused") and not may), {
            "attempt": attempt, "may_write": may}

    def write_denied(self, side):
        outcomes = {w: self._denied(side, w) for w in self.writers}
        return all(d for d, _ in outcomes.values()), {
            "probe": "write_denied", "side": side, "database": self.databases[side],
            "outcomes": {w: detail for w, (_, detail) in outcomes.items()}}

    def single_writer(self):
        wrote, target = self._attempt("target", self.app_user, self.databases["target"])
        denied, source = self.write_denied("source")
        return wrote and denied, {"probe": "single_writer", "target": target,
                                  "source": source["outcomes"]}

    # -- content -----------------------------------------------------------------
    @staticmethod
    def _digest(database, collection):
        from bson import json_util

        h, count = hashlib.sha256(), 0
        for document in database[collection].find({}, sort=[("_id", 1)]):
            h.update(json_util.dumps(document, json_options=json_util.CANONICAL_JSON_OPTIONS)
                     .encode() + b"\n")
            count += 1
        indexes = sorted(repr((ix["name"], sorted(ix["key"].items()), ix.get("unique"),
                               ix.get("expireAfterSeconds")))
                         for ix in database[collection].list_indexes())
        h.update(repr(indexes).encode())
        return count, h.hexdigest()

    def content_equal(self, name):
        if name not in self.collections:
            raise ContractError("Unregistered protected set")
        source = self._digest(self.connect("source")[self.databases["source"]], name)
        target = self._digest(self.connect("target")[self.databases["target"]], name)
        return source == target and source[0] > 0, {
            "probe": "content_equal", "set": name,
            "source": {"documents": source[0], "digest": source[1]},
            "target": {"documents": target[0], "digest": target[1]}}

    def invariant(self, invariant_id):
        spec = self.invariants.get(invariant_id)
        if spec is None:
            return False, {"probe": "invariant", "invariant": invariant_id,
                           "reason": "no probe registered for this invariant"}
        collection = self.connect("target")[self.databases["target"]][spec["collection"]]
        if "unique" in spec:
            fields = list(spec["unique"])
            pipeline = [{"$group": {"_id": {f: f"${f}" for f in fields},
                                    "n": {"$sum": 1}}}, {"$match": {"n": {"$gt": 1}}},
                        {"$limit": 20}]
        elif "violations" in spec:
            pipeline = [*spec["violations"], {"$limit": 20}]
        else:
            raise ContractError("Unknown invariant type")
        found = list(collection.aggregate(pipeline))
        checked = collection.estimated_document_count()
        return checked > 0 and not found, {"probe": "invariant", "invariant": invariant_id,
                                           "documents": checked,
                                           "violations": [str(f.get("_id")) for f in found]}

    # -- isolation, transport, placement ---------------------------------------------------
    def cross_tenant_denied(self, other=None):
        import pymongo

        database = other or self.other_db
        if not database:
            return False, {"probe": "cross_tenant_denied", "reason": "no other tenant registered"}
        client = self.connect_as("target", self.app_user)
        try:
            client[database].list_collection_names()
            outcome = "read_allowed"
        except pymongo.errors.OperationFailure as error:
            outcome = "refused" if error.code in (13, 18) else f"undecided:{error.code}"
        finally:
            client.close()
        return outcome == "refused", {"probe": "cross_tenant_denied", "other_db": database,
                                      "outcome": outcome}

    def transport_encrypted(self, side):
        options = self.connect(side).options.pool_options
        tls = options._ssl_context is not None
        context = options._ssl_context
        verified = bool(tls and getattr(context, "verify_mode", 0) == 2
                        and getattr(context, "check_hostname", False))
        return verified, {"probe": "transport_encrypted", "side": side, "tls": tls,
                          "client_verification": "verify-identity" if verified else "none"}

    def zone(self, side, allowed):
        label = self.zone_of(side) if self.zone_of else None
        return label in allowed, {"probe": "zone", "side": side, "label": label,
                                  "allowed": sorted(allowed),
                                  "source": "inventory" if self.zone_of else "none"}

    # -- restore -----------------------------------------------------------------------
    def restore_drill(self, scratch=None):
        """Copy the target's collections into a scratch database, compare, drop it."""
        from bson.raw_bson import RawBSONDocument

        client = self.connect("target")
        target, name = client[self.databases["target"]], f"{scratch or 'sova_restore'}"
        if name in client.list_database_names():
            return False, {"probe": "restore_drill", "reason": f"scratch database {name} exists"}
        copy = client[name]
        raw = target.codec_options.with_options(document_class=RawBSONDocument)
        sets = {}
        try:
            for collection in self.collections:
                for index in target[collection].list_indexes():
                    if index["name"] != "_id_":
                        spec = {k: v for k, v in index.items() if k not in ("v", "ns", "key")}
                        copy[collection].create_index(list(index["key"].items()), **spec)
                documents = list(target.get_collection(collection, codec_options=raw).find(
                    {}, sort=[("_id", 1)]))
                if documents:
                    copy[collection].insert_many(documents, ordered=True)
                sets[collection] = self._digest(target, collection) == \
                    self._digest(copy, collection)
        finally:
            client.drop_database(name)
        return all(sets.values()) and bool(sets), {"probe": "restore_drill",
                                                   "scratch_db": name, "collections": sets}
