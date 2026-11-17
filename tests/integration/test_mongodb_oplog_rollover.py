"""A change stream whose history the source's oplog no longer holds: the
oplog is a capped collection, and a source busy enough between the sync and
the write stop overwrites the point the kept resume token stands for. The
final step then makes a full copy of the fenced source instead, and says so.

Measured on a real rollover: the test servers' oplog is 128 MB, filled here
by documents written and removed outside the unit (as another application
on the same server would)."""

from datetime import UTC, datetime
import time

from adapter_conformance import apply, command
from bson.timestamp import Timestamp
import pytest
from test_mongodb_change_stream import ChangeStreamWorld

from sova.adapters.mongodb_change_stream import _LOST, token_time

pytestmark = [pytest.mark.mongodb, pytest.mark.kubernetes]

FILLER = 8 << 20  # bytes per filler document


def oldest(admin):
    first = admin.local["oplog.rs"].find({}, {"ts": 1}).sort("$natural", 1).limit(1)
    return next(iter(first))["ts"]


def passed(admin, point):
    first = oldest(admin)
    return (first.time, first.inc) > point


def roll_past(admin, database, point, *, limit_bytes=1 << 30, seconds=180):
    """Write and remove filler documents until the oldest oplog entry is after
    ``point`` (a (time, inc) pair); the bytes it took. The oplog is truncated
    only behind the last checkpoint (it replays from there after a crash),
    so each round ends with one (``fsync``) and a wait for the truncation."""
    filler, written = admin[database]["filler"], 0
    deadline = time.monotonic() + seconds
    while not passed(admin, point):
        assert written < limit_bytes and time.monotonic() < deadline, (written, oldest(admin))
        for _ in range(8):  # 64 MiB a round, half the oplog
            filler.insert_one({"_id": written, "blob": b"\0" * FILLER})
            filler.delete_one({"_id": written})
            written += FILLER
        admin.admin.command("fsync")
        settle = time.monotonic() + 10
        while not passed(admin, point) and time.monotonic() < settle:
            time.sleep(0.5)
    return written


def test_a_history_the_oplog_no_longer_holds_ends_in_a_full_copy(mongo_business):
    world = ChangeStreamWorld(mongo_business)
    admin = world.b["admin"]["source"]
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    marker = world.b["admin"]["target"][world.names["tgt"]]["__sova_sync"].find_one(
        {"_id": "sync"})
    kept = token_time(marker["resume_token"])
    assert kept is not None
    world.business_write(3)  # changes the stream would have carried
    rolled = roll_past(admin, world.names["other"], kept)
    assert oldest(admin) > Timestamp(*kept)
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes"):
        apply(world.adapter(), action)
    seen = world.adapter().readback(command("mongodb-handoff", "apply_final_changes"),
                                    datetime.now(UTC)).evidence
    assert seen["brought_level_by"] == "full_copy", seen
    code = int(seen["reason"].rsplit(":", 1)[1])
    assert seen["reason"].startswith("resume_refused:") and code in _LOST, seen
    assert world.content("source") == world.content("target")
    print(f"rolled {rolled >> 20} MiB; resume refused with {code}")
