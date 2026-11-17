"""Reference adapter: a MongoDB database of an application moved to another
deployment (§16.1, §16.5).

The unit is the application's registered collections in one database; the
writers are MongoDB users (``user@authdb``). The same actions and
postconditions as the other data adapters, read back from the servers:

* Pausing admission restricts the application user's authentication to an
  address no client has (``authenticationRestrictions``, TEST-NET-1): no
  connection can log in as it, and its roles stay for the fence to take.
  MongoDB has no account lock; this is stricter than one: the server re-checks
  a changed user on connections already open, which lose their login at their
  next command (measured).
* The fence revokes every role a writer holds on the source database,
  restricts every writer's login the same way, and ends their running
  operations (``killOp``). Connections already open lose the rights at their
  next command, as at the pause (ending sessions by user would need
  ``impersonate``, which not even ``root`` has). It reads back as done only when, besides, no other user
  (``usersInfo`` with privileges, the server's own view, roles inherited
  included) may write the source database, and no operation of a writer is
  still running.
* Staging creates the target collections with the source's options and
  indexes; the writers exist on the target (the operator creates accounts, as
  for MySQL) and hold no role on the target database. Activation grants the
  application user ``readWrite`` there and lifts its restriction.
* Documents are copied as raw BSON in ``_id`` order and compared as raw BSON:
  equal bytes are equal documents, field order and types included. A TTL
  index's expired documents are removed by each server on its own schedule
  (every 60 seconds), so the two sides may differ in them for a while: a
  comparison leaves out, on both sides alike, the documents a TTL index
  removes before the comparison's time plus a margin (``ttl_live``), and
  says how many.
  ``start_sync`` copies while the source still serves and leaves a marker
  collection outside the unit.

Database and collection names are plain identifiers. Every action is
idempotent; a postcondition that does not hold reads back UNKNOWN.
"""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import re

from sova.adapters.evidence_bounds import bounded
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
WRITE_ACTIONS = frozenset({"insert", "update", "remove", "dropCollection", "dropDatabase",
                           "createCollection", "createIndex", "dropIndex", "renameCollectionSameDB",
                           "collMod", "convertToCapped", "emptycapped", "bypassDocumentValidation"})
NOWHERE = [{"clientSource": ["192.0.2.1/32"]}]  # TEST-NET-1: no client logs in from there
MARKER = "__sova_sync"
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_USER = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
_BATCH = 1000
TTL_MARGIN_SECONDS = 120  # the TTL monitor's 60 s cycle, and the clocks' skew


def name(value):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise ContractError("Plain MongoDB name required")
    return value


def account(value):
    """``user@authdb`` -> (user, authdb), validated."""
    if type(value) is not str or "@" not in value:
        raise ContractError("Writer as user@authdb required")
    user, db = value.rsplit("@", 1)
    if not _USER.fullmatch(user):
        raise ContractError("Plain MongoDB user name required")
    return user, name(db)


def ttl_live(collection, now):
    """(filter, expiring): the documents of ``collection`` no TTL index removes
    before ``now`` plus the margin, and the filter for the ones it may. A
    document expires when its indexed date is older than ``expireAfterSeconds``
    (the earliest, for an array) and it matches the index's partial filter;
    ``$lt`` compares dates with dates only, and any element of an array."""
    expiring = []
    for index in collection.list_indexes():
        expire = index.get("expireAfterSeconds")
        if expire is None or len(index["key"]) != 1:
            continue
        field = next(iter(index["key"]))
        rule = {field: {"$lt": now - timedelta(seconds=float(expire) - TTL_MARGIN_SECONDS)}}
        partial = index.get("partialFilterExpression")
        expiring.append({"$and": [dict(partial), rule]} if partial else rule)
    if not expiring:
        return {}, None
    return {"$nor": expiring}, {"$or": expiring}


def shard_keys(client, database, collections):
    """collection -> its shard key, for the sharded ones (read from the cluster's
    own catalogue; a replica set has none)."""
    names = [f"{database}.{c}" for c in collections]
    found = client["config"]["collections"].find({"_id": {"$in": names}, "dropped": {"$ne": True}})
    return {entry["_id"].split(".", 1)[1]: dict(entry["key"]) for entry in found}


def placement_index(index, keys):
    """Whether an index exists to place documents on shards (it serves the
    collection's shard key on either side) rather than to serve the business:
    such an index is left out of comparisons."""
    return index["name"] != "_id_" and dict(index["key"]) in keys


def may_write(privileges, database):
    """Whether a privilege list lets its holder change ``database``."""
    for privilege in privileges or ():
        resource = privilege.get("resource") or {}
        if resource.get("anyResource") or resource.get("cluster"):
            if set(privilege.get("actions") or ()) & WRITE_ACTIONS:
                return True
            continue
        if resource.get("db") in (database, "") and \
                set(privilege.get("actions") or ()) & WRITE_ACTIONS:
            return True
    return False


class MongoDatabaseHandoffAdapter:
    def __init__(self, adapter_id, *, connect, source_db, target_db, collections, writers,
                 app_user, admin_user, route):
        """``connect(side)`` gives an administrative ``MongoClient`` on that side's
        deployment; ``admin_user`` (``user@authdb``) is the one Sova uses."""
        if not callable(connect) or not all(callable(getattr(route, n, None))
                                            for n in ("get", "set")):
            raise ContractError("Administrative connect and a route store required")
        if type(collections) is not tuple or not collections:
            raise ContractError("The unit's collections required")
        if type(writers) is not tuple or app_user not in writers:
            raise ContractError("The application user must be a registered writer")
        self.adapter_id, self.connect, self.route = adapter_id, connect, route
        self.source_db, self.target_db = name(source_db), name(target_db)
        self.collections = tuple(name(c) for c in collections)
        self.writers = tuple(writers)
        for writer in self.writers:
            account(writer)
        self.app_user, self.admin_user = app_user, account(admin_user)

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

    # -- users -----------------------------------------------------------------
    def _db(self, side):
        return self.source_db if side == "source" else self.target_db

    def _user(self, side, writer):
        user, db = account(writer)
        found = self.connect(side)[db].command(
            "usersInfo", {"user": user, "db": db}, showPrivileges=True,
            showAuthenticationRestrictions=True)["users"]
        return found[0] if found else None

    def _users(self, side):
        """Every user of the side's deployment that may write its database."""
        client, database = self.connect(side), self._db(side)
        found = []
        for db in sorted({"admin", database}):
            for listed in client[db].command("usersInfo")["users"]:
                # privileges come only with an exact-match query, one user at a time
                user = client[db].command("usersInfo", {"user": listed["user"], "db": db},
                                          showPrivileges=True)["users"]
                if user and may_write(user[0].get("inheritedPrivileges"), database):
                    found.append(f"{listed['user']}@{db}")
        return found

    def _restricted(self, user):
        return (user or {}).get("authenticationRestrictions") in (NOWHERE, [NOWHERE])

    def _restrict(self, side, writer, restrictions):
        user, db = account(writer)
        self.connect(side)[db].command("updateUser", user,
                                       authenticationRestrictions=restrictions)

    def _revoke_on(self, side, writer):
        user, db = account(writer)
        found = self._user(side, writer)
        if found is None:
            raise ContractError(f"Writer {writer} does not exist on the {side}")
        roles = [r for r in found.get("roles") or () if r["db"] == self._db(side)]
        if roles:
            self.connect(side)[db].command("revokeRolesFromUser", user, roles=roles)

    def _operations(self, side):
        names = {account(w)[0] for w in self.writers}
        ops = self.connect(side).admin.command("currentOp", True).get("inprog") or []
        return [{"opid": op.get("opid"), "user": u["user"]} for op in ops
                for u in op.get("effectiveUsers") or () if u.get("user") in names]

    # -- content -----------------------------------------------------------------
    def _shard_keys(self):
        """collection -> the shard keys either side shards it by."""
        out = {}
        for side in ("source", "target"):
            for collection, key in shard_keys(self.connect(side), self._db(side),
                                              self.collections).items():
                out.setdefault(collection, []).append(key)
        return out

    def _spec(self, side, keys=None):
        """collection -> (options, index specs) of the unit, or None if missing;
        an index that serves a shard key (``keys``) on either side is placement,
        not business, and left out."""
        keys = self._shard_keys() if keys is None else keys
        database = self.connect(side)[self._db(side)]
        listed = {c["name"]: c.get("options") or {}
                  for c in database.list_collections(filter={"name": {"$in": list(
                      self.collections)}})}
        out = {}
        for collection in self.collections:
            if collection not in listed:
                out[collection] = None
                continue
            indexes = sorted(json.dumps({k: v for k, v in ix.items() if k not in ("v", "ns")},
                                        sort_keys=True, default=str)
                             for ix in database[collection].list_indexes()
                             if not placement_index(ix, keys.get(collection, ())))
            out[collection] = (json.dumps(listed[collection], sort_keys=True, default=str),
                               indexes)
        return out

    def _digest(self, side, now=None):
        from bson.raw_bson import RawBSONDocument

        database = self.connect(side)[self._db(side)]
        now = now or datetime.now(UTC)
        out = {}
        for collection in self.collections:
            h, count = hashlib.sha256(), 0
            live, expiring = ttl_live(database[collection], now)
            raw = database.get_collection(collection).with_options(
                codec_options=database.codec_options.with_options(
                    document_class=RawBSONDocument))
            for document in raw.find(live, sort=[("_id", 1)], batch_size=_BATCH):
                h.update(document.raw)
                count += 1
            out[collection] = {"documents": count, "digest": h.hexdigest()}
            if expiring is not None:  # left out alike on both sides
                out[collection]["ttl_expiring"] = database[collection].count_documents(
                    expiring)
        return out

    # -- actions ---------------------------------------------------------------
    def _create_like_source(self):
        source, target = self.connect("source")[self.source_db], \
            self.connect("target")[self.target_db]
        options = {c["name"]: c.get("options") or {}
                   for c in source.list_collections(filter={"name": {"$in": list(
                       self.collections)}})}
        existing = set(target.list_collection_names())
        for collection in self.collections:
            if collection not in options:
                raise ContractError(f"Source collection {collection} does not exist")
            if collection not in existing:
                target.create_collection(collection, **options[collection])
            for index in source[collection].list_indexes():
                if index["name"] == "_id_":
                    continue
                spec = {k: v for k, v in index.items() if k not in ("v", "ns", "key")}
                target[collection].create_index(list(index["key"].items()), **spec)

    def _stage_target(self):
        self._create_like_source()
        for writer in self.writers:
            self._revoke_on("target", writer)
            self._restrict("target", writer, NOWHERE)

    def _check_stage_target(self):
        return self._staged("target", "source")

    def _staged(self, side, other):
        keys = self._shard_keys()
        mine, theirs = self._spec(side, keys), self._spec(other, keys)
        present = all(v is not None for v in mine.values())
        writers = {w: (self._user(side, w) or {}).get("inheritedPrivileges") for w in self.writers}
        writing = sorted(w for w, privileges in writers.items()
                         if privileges is None or may_write(privileges, self._db(side)))
        return present and mine == theirs and not writing, {
            "collections_present": present, "same_definitions": mine == theirs,
            "writers_that_may_write": writing}

    def _pause_admission(self):
        self._restrict("source", self.app_user, NOWHERE)

    def _check_pause_admission(self):
        restricted = self._restricted(self._user("source", self.app_user))
        return restricted, {"application_login_restricted": restricted}

    def _fence_source_writes(self):
        client = self.connect("source")
        for writer in self.writers:
            self._revoke_on("source", writer)
            self._restrict("source", writer, NOWHERE)
        for op in self._operations("source"):
            client.admin.command("killOp", op=op["opid"])

    def _check_fence_source_writes(self):
        return self._fenced("source")

    def _fenced(self, side):
        writers = {w: self._user(side, w) for w in self.writers}
        open_ = sorted(w for w, user in writers.items()
                       if user is None or may_write(user.get("inheritedPrivileges"),
                                                    self._db(side))
                       or not self._restricted(user))
        admin = "@".join(self.admin_user)
        others = sorted(u for u in self._users(side) if u not in self.writers and u != admin)
        ops = self._operations(side)
        return not open_ and not others and not ops, {
            "writers_not_fenced": open_, "other_users_that_may_write": others[:20],
            "writer_operations": ops[:20]}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        return self._fenced("target")

    def _source_read_concern(self):
        return None  # the database's own; the source is fenced when this copy counts

    def _copy(self):
        from bson.raw_bson import RawBSONDocument

        self._create_like_source()
        source, target = self.connect("source")[self.source_db], \
            self.connect("target")[self.target_db]
        raw = source.codec_options.with_options(document_class=RawBSONDocument)
        for collection in self.collections:
            target[collection].delete_many({})
            batch = []
            for document in source.get_collection(
                    collection, codec_options=raw,
                    read_concern=self._source_read_concern()).find(
                    {}, sort=[("_id", 1)], batch_size=_BATCH):
                batch.append(document)
                if len(batch) >= _BATCH:
                    target[collection].insert_many(batch, ordered=True)
                    batch = []
            if batch:
                target[collection].insert_many(batch, ordered=True)

    def _start_sync(self):
        self._copy()
        self.connect("target")[self.target_db][MARKER].replace_one(
            {"_id": "sync"}, {"_id": "sync", "at": datetime.now(UTC).isoformat()}, upsert=True)

    def _check_start_sync(self):
        marked = self.connect("target")[self.target_db][MARKER].find_one({"_id": "sync"})
        return marked is not None, {"synced": marked is not None}

    def _copy_content(self):
        self._copy()

    def _check_copy_content(self):
        return self._compare()

    def _compare(self):
        now = datetime.now(UTC)  # one cut for both sides
        source, target = self._digest("source", now), self._digest("target", now)
        same = all({k: v for k, v in source[c].items() if k != "ttl_expiring"} ==
                   {k: v for k, v in target[c].items() if k != "ttl_expiring"}
                   for c in self.collections)
        keys = self._shard_keys()
        return same and self._spec("source", keys) == self._spec("target", keys), {
            "source": source, "target": target}

    def _apply_final_changes(self):
        self._copy()

    def _check_apply_final_changes(self):
        return self._compare()

    def _verify_final_state(self):
        return None  # measurement only; the readback is the comparison

    def _check_verify_final_state(self):
        return self._compare()

    def _activate_target(self):
        user, db = account(self.app_user)
        client = self.connect("target")
        client[db].command("grantRolesToUser", user,
                           roles=[{"role": "readWrite", "db": self.target_db}])
        self._restrict("target", self.app_user, [])

    def _check_activate_target(self):
        app = self._user("target", self.app_user)
        enabled = app is not None and not self._restricted(app) and may_write(
            app.get("inheritedPrivileges"), self.target_db)
        others = sorted(w for w in self.writers if w != self.app_user and may_write(
            (self._user("target", w) or {}).get("inheritedPrivileges"), self.target_db))
        fenced, source = self._fenced("source")
        return enabled and not others and fenced, {
            "application_enabled": enabled, "other_writers_that_may_write": others,
            "source": source}

    def _commit_route(self):
        self.route.set(self.target_db)

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.target_db, {"route": current}
