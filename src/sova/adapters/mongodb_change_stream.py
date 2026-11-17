"""Reference adapter: a MongoDB database moved by snapshot and change capture
through the source's own change stream (§16.1 snapshot-cdc), between two
deployments.

Writers, the fence, activation and comparison are the copy adapter's. What
changes is when the documents move, so the write stop no longer grows with
the data:

* ``start_sync`` reads the source's majority-committed point, copies the
  documents (majority reads, all from after that point) while the source
  keeps serving, then applies the change stream's events from that point up
  to the source's majority-committed point now. The stream's resume token is
  kept on the target, in the marker collection outside the unit.
* Applying an event is idempotent by document key, so a document ends as the
  source's last version of it once the events are applied in order:
  * an insert or a replace writes the event's document;
  * an update writes the document as the source holds it now
    (``updateLookup``), or removes it if the source no longer has it;
  * a delete removes it.
  Documents keep their raw BSON, field order and types included.
* ``apply_final_changes`` runs once the source is fenced:
  1. It reads the source's last write and waits, bounded, until that write is
     majority-committed. A change stream shows only majority-committed changes.
  2. It resumes the stream from the kept token and applies events until the
     stream has passed that write. The point a resume token stands for is
     read from the token itself.
  3. It creates any index the source gained meanwhile.
  It reads back as done only when content and definitions are equal.
  A full copy is made here instead in two cases: the sync never ran, or the
  source refuses to resume from the token (its oplog no longer holds it, or
  the token is not one of this stream's). The source is fenced at that
  point, and the readback says which way the target was brought level.
* Dropping or renaming a collection of the unit, or dropping the database,
  while the stream runs ends the sync with an error. That is not a change to
  carry.
* An event over the BSON size limit (an update of a large document carries
  the whole document and the change) arrives split in fragments
  (``$changeStreamSplitLargeEvent``); the fragments are joined and applied
  as one event, and a rerun resumes only from a whole event.

The source must be a replica set; a standalone server has no change stream.
The stream only reads the oplog and keeps nothing on the source, so an abort
has nothing to release.
"""

from datetime import UTC, datetime
import time

from sova.adapters.mongodb_handoff import MARKER, MongoDatabaseHandoffAdapter
from sova.domain.contracts import ContractError

_LOST = frozenset({280, 286})  # ChangeStreamFatalError, ChangeStreamHistoryLost
_DDL = frozenset({"drop", "rename", "dropDatabase", "invalidate"})
_KEEP_EVERY = 1000


def token_time(token):
    """The cluster time ``(seconds, increment)`` a resume token stands for, or
    None. A token's ``_data`` is a key string whose first value is that time
    (type byte 0x82, then the timestamp big-endian), as other change capture
    tools read it."""
    data = token.get("_data") if hasattr(token, "get") else None
    if type(data) is not str or len(data) < 18 or data[:2].lower() != "82":
        return None
    try:
        raw = bytes.fromhex(data[2:18])
    except ValueError:
        return None
    return int.from_bytes(raw[:4], "big"), int.from_bytes(raw[4:], "big")


class MongoChangeStreamHandoffAdapter(MongoDatabaseHandoffAdapter):
    def __init__(self, adapter_id, *, catch_up_seconds=120, **common):
        super().__init__(adapter_id, **common)
        if type(catch_up_seconds) not in (int, float) or not 0 < catch_up_seconds <= 3600:
            raise ContractError("catch_up_seconds: more than 0, at most 3600")
        self.catch_up_seconds = catch_up_seconds

    # -- the stream -------------------------------------------------------------
    def _source_read_concern(self):
        from pymongo.read_concern import ReadConcern

        return ReadConcern("majority")  # never copy what a failover could roll back

    def _hello(self):
        hello = self.connect("source").admin.command("hello")
        if not hello.get("setName"):
            raise ContractError("Change capture needs the source to be a replica set")
        return hello

    def _committed_at(self):
        """The source's majority-committed point (a BSON timestamp)."""
        return self._hello()["lastWrite"]["majorityOpTime"]["ts"]

    def _committed(self):
        """The source's majority-committed point, as (seconds, increment)."""
        ts = self._committed_at()
        return ts.time, ts.inc

    def _last_write_committed(self):
        """Wait (bounded) until the source's last write is majority-committed;
        returns that write's time."""
        deadline = time.monotonic() + self.catch_up_seconds
        written = self._hello()["lastWrite"]["opTime"]["ts"]
        while True:
            committed = self._committed()
            if committed >= (written.time, written.inc):
                return written.time, written.inc
            if time.monotonic() > deadline:
                raise ContractError("The source's last write was not majority-committed in time")
            time.sleep(0.2)

    def _stream(self, token=None, at=None):
        """The unit's change stream after ``token``, or from cluster time ``at``."""
        from bson.raw_bson import RawBSONDocument

        database = self.connect("source")[self.source_db]
        raw = database.with_options(codec_options=database.codec_options.with_options(
            document_class=RawBSONDocument))
        unit = list(self.collections)
        pipeline = [{"$match": {"$or": [{"ns.coll": {"$in": unit}}, {"to.coll": {"$in": unit}},
                                        {"operationType": {"$in": ["dropDatabase",
                                                                   "invalidate"]}}]}}]
        pipeline.append({"$changeStreamSplitLargeEvent": {}})  # must be the last stage
        return raw.watch(pipeline, full_document="updateLookup", start_after=token,
                         start_at_operation_time=at, max_await_time_ms=100)

    @staticmethod
    def _joined(fragments):
        """One event from its fragments: their top-level fields together (a
        field is never split; each fragment holds whole fields)."""
        event = {}
        for fragment in fragments:
            event.update({k: fragment[k] for k in fragment if k not in ("_id", "splitEvent")})
        return event

    def _apply(self, target, event):
        kind = event["operationType"]
        if kind in _DDL:
            raise ContractError(f"The source's {kind} during the sync is not a change to carry")
        collection = target[event["ns"]["coll"]]
        key, document = event["documentKey"], event.get("fullDocument")
        if kind in ("insert", "replace", "update") and document is not None:
            collection.replace_one(key, document, upsert=True)
        elif kind in ("delete", "update"):  # an update whose document is gone since
            collection.delete_one(key)
        else:
            raise ContractError(f"Unexpected change event {kind}")

    def _drain(self, token, until, *, at=None):
        """Apply events after ``token`` (or from cluster time ``at``) until the
        stream has passed ``until``; returns (token, events applied).

        Passed means an event later than ``until`` (the events of one
        transaction share its time, so an event at ``until`` may have more
        behind it), or an empty batch whose token is at or after it. A source
        that keeps serving never gives an empty batch: waiting for one would
        chase its writes forever."""
        target = self.connect("target")[self.target_db]
        deadline, applied, fragments = time.monotonic() + self.catch_up_seconds, 0, []
        with self._stream(token, at) as stream:
            while True:
                event = stream.try_next()
                reached = token_time(stream.resume_token)
                if event is not None and event.get("splitEvent") is not None:
                    fragments.append(event)
                    split = event["splitEvent"]
                    if split["fragment"] < split["of"]:
                        event = None  # not whole yet: nothing applied, nothing kept
                    else:
                        event, fragments = self._joined(fragments), []
                if fragments:  # inside a split event: neither stop nor keep here
                    pass
                elif event is not None:
                    self._apply(target, event)
                    applied += 1
                    if applied % _KEEP_EVERY == 0:  # a rerun resumes from here
                        self._keep(stream.resume_token, "change_stream")
                    if reached is not None and reached > until:
                        return stream.resume_token, applied
                elif reached is not None and reached >= until:
                    return stream.resume_token, applied
                if time.monotonic() > deadline:
                    raise ContractError("The change stream did not reach the source's "
                                        "last write in time")

    def _keep(self, token, mode, **detail):
        self.connect("target")[self.target_db][MARKER].replace_one(
            {"_id": "sync"}, {"_id": "sync", "at": datetime.now(UTC).isoformat(),
                              "mode": mode, "resume_token": token, **detail}, upsert=True)

    def _marker(self):
        return self.connect("target")[self.target_db][MARKER].find_one({"_id": "sync"})

    # -- actions ---------------------------------------------------------------
    def _start_sync(self):
        # The copy reads majority-committed data from after this point, and the
        # stream starts at it: nothing slips between. (A stream opened here and
        # asked for its token gives none when its first batch already holds
        # events, as on a busy source: a start of "now" would then lose the
        # changes made during the copy.) Changes at the point itself are
        # applied again; applying is idempotent.
        start = self._committed_at()
        self._copy()
        token, applied = self._drain(None, self._committed(), at=start)
        self._keep(token, "change_stream", applied_at_sync=applied)

    def _check_start_sync(self):
        marked = self._marker() or {}
        following = marked.get("mode") == "change_stream" and \
            token_time(marked.get("resume_token")) is not None
        return following, {"synced": bool(marked), "following": following,
                           "applied_at_sync": marked.get("applied_at_sync")}

    def _apply_final_changes(self):
        import pymongo

        token = (self._marker() or {}).get("resume_token")
        if token is None:  # the sync never ran here: copy everything instead
            self._copy()
            self._keep(None, "full_copy", reason="sync_not_run")
            return
        until = self._last_write_committed()
        try:
            token, applied = self._drain(token, until)
        except pymongo.errors.OperationFailure as error:
            if error.code not in _LOST:
                raise
            self._copy()  # the source is fenced: a full copy is exact
            self._keep(None, "full_copy", reason=f"resume_refused:{error.code}")
            return
        self._create_like_source()
        self._keep(token, "change_stream", applied_at_final=applied)

    def _check_apply_final_changes(self):
        equal, detail = self._compare()
        marked = self._marker() or {}
        return equal, {**detail, "brought_level_by": marked.get("mode"),
                       "reason": marked.get("reason"),
                       "applied_at_final": marked.get("applied_at_final")}
