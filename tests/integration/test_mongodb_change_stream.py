"""MongoDB change capture through the source's change stream, against the
conformance kit, and what it adds:
* a write stop that carries only what changed since the sync;
* changes made while the copy runs reach the target;
* a structural change ends the sync instead of being carried;
* the full copy a lost history falls back to (the server's own error);
* the whole control plane running a snapshot-cdc plan on it."""

import json
import threading
import time

from datetime import UTC, datetime

from adapter_conformance import HandoffAdapterConformance, apply, command, readback
from control_plane_support import prepared, signed_results, worker_for
from handoff_drive import without_transport_control
from mongodb_business import as_user
from mongodb_world import COLLECTIONS, MongoWorld, Route
import pytest

from sova.adapters.mongodb_change_stream import (
    _LOST,
    MongoChangeStreamHandoffAdapter,
    token_time,
)
from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError, EffectStatus
from sova.domain.handoff_authority import HandoffState
from sova.service.mongodb_execution import mongodb_factory

pytestmark = [pytest.mark.mongodb, pytest.mark.kubernetes]


class ChangeStreamWorld(MongoWorld):
    def _adapter(self, source, target):
        clients = {"source": self.b["admin"][source], "target": self.b["admin"][target]}
        names = {"source": self._db(source), "target": self._db(target)}
        return MongoChangeStreamHandoffAdapter(
            "mongodb-handoff", connect=lambda side: clients[side], source_db=names["source"],
            target_db=names["target"], collections=COLLECTIONS, writers=self.b["writers"],
            app_user=f"{self.users['app']}@admin", admin_user="sova-admin@admin",
            route=self._route, catch_up_seconds=60)


@pytest.fixture
def world(mongo_business):
    return ChangeStreamWorld(mongo_business)


class TestMongoChangeStreamConformance(HandoffAdapterConformance):
    pass


def bulk(world, documents):
    app = as_user(world.b, "source", world.users["app"])[world.names["src"]]
    for start in range(0, documents, 5000):
        app.orders.insert_many([{"_id": 1000 + i, "customer": f"bulk-{i % 50}",
                                 "idempotency_key": f"bulk-{i}", "amount": i}
                                for i in range(start, min(start + 5000, documents))])


def written_on_target(world):
    found = world.b["admin"]["target"].admin.command("serverStatus")["metrics"]["document"]
    return found["inserted"] + found["updated"] + found["deleted"]


def window(world, steps):
    """(seconds, documents the target server wrote) from the pause to the verified state."""
    written, started = written_on_target(world), time.monotonic()
    for action in ("pause_admission", "fence_source_writes", *steps, "verify_final_state"):
        apply(world.adapter(), action)
    return time.monotonic() - started, written_on_target(world) - written


def test_the_write_stop_carries_only_what_changed_since_the_sync(world):
    bulk(world, 20000)
    apply(world.adapter(), "stage_target")
    started = time.monotonic()
    apply(world.adapter(), "start_sync")
    synced = time.monotonic() - started
    assert readback(world.adapter(), "start_sync") == EffectStatus.APPLIED
    world.business_write(5)  # the source still serves; the stream carries it
    world.business_delete(2)
    spent, documents = window(world, ("apply_final_changes",))
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    assert world.content("source") == world.content("target")
    assert documents < 100, documents  # the bulk was not copied again inside the stop
    print(f"change stream: initial sync {synced:.1f}s; stop {spent:.2f}s, "
          f"{documents} documents written in it")


def test_an_offline_copy_writes_everything_inside_the_stop(world):
    """The same business without a sync: the contrast the template choice is about."""
    bulk(world, 20000)
    apply(world.adapter(), "stage_target")
    spent, documents = window(world, ("copy_content",))
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED
    assert documents >= 20000, documents
    print(f"offline copy: stop {spent:.2f}s, {documents} documents written in it")


def test_changes_made_while_the_copy_runs_reach_the_target(world):
    bulk(world, 5000)
    apply(world.adapter(), "stage_target")
    stop, done = threading.Event(), {"writes": 0}

    def business():  # inserts, updates and deletes all through the copy
        app = as_user(world.b, "source", world.users["app"])[world.names["src"]]
        i = 0
        while not stop.is_set():
            app.orders.insert_one({"_id": 100000 + i, "customer": "live",
                                   "idempotency_key": f"live-{i}", "amount": i})
            app.orders.update_one({"_id": 1000 + (i * 13) % 5000}, {"$inc": {"amount": 1}})
            app.orders.delete_one({"_id": 1000 + (i * 7919) % 5000})
            app.idempotency_results.replace_one({"_id": f"live:{i % 10}"},
                                                {"_id": f"live:{i % 10}", "result": str(i)},
                                                upsert=True)
            done["writes"] += 4
            i += 1

    writer = threading.Thread(target=business)
    writer.start()
    try:
        time.sleep(0.5)
        apply(world.adapter(), "start_sync")
        time.sleep(0.5)
    finally:
        stop.set()
        writer.join(30)
    assert done["writes"] > 0
    window(world, ("apply_final_changes",))
    observation = world.adapter().readback(
        command("mongodb-handoff", "apply_final_changes"), datetime.now(UTC))
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    assert world.content("source") == world.content("target")
    print(f"writes while syncing: {done['writes']}")


def test_a_dropped_collection_ends_the_sync(world):
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    world.b["admin"]["source"][world.names["src"]].idempotency_results.drop()
    for action in ("pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    with pytest.raises(ContractError, match="drop"):
        apply(world.adapter(), "apply_final_changes")
    assert readback(world.adapter(), "apply_final_changes") == EffectStatus.UNKNOWN


def test_a_stream_that_cannot_resume_falls_back_to_a_full_copy(world):
    """A kept token the source cannot resume from (here: another database's
    event - the server's own refusal, as for one the oplog no longer holds)
    ends in a full copy of the fenced source, said so in the readback."""
    import pymongo

    admin = world.b["admin"]["source"]
    other = admin[world.names["other"]]
    with other.watch() as stream:
        other.shop.insert_one({"_id": 2, "tenant": "other"})
        foreign = stream.next()["_id"]
    # The server finds a token missing at the stream's first event after it.
    world.business_write(1)
    with pytest.raises(pymongo.errors.OperationFailure) as refused:
        with admin[world.names["src"]].watch(start_after=foreign) as stream:
            stream.try_next()
    assert refused.value.code in _LOST, refused.value.code
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    world.business_write(3)
    for action in ("pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    world.b["admin"]["target"][world.names["tgt"]]["__sova_sync"].update_one(
        {"_id": "sync"}, {"$set": {"resume_token": foreign}})
    apply(world.adapter(), "apply_final_changes")
    observation = world.adapter().readback(
        command("mongodb-handoff", "apply_final_changes"), datetime.now(UTC)).evidence
    assert observation["brought_level_by"] == "full_copy" and \
        observation["reason"] == f"resume_refused:{refused.value.code}", observation
    assert world.content("source") == world.content("target")


def test_a_resume_token_carries_its_cluster_time(world):
    source = world.b["admin"]["source"][world.names["src"]]
    with source.watch() as stream:
        before = token_time(stream.resume_token)
    world.business_write(1)
    hello = world.b["admin"]["source"].admin.command("hello")
    written = hello["lastWrite"]["opTime"]["ts"]
    assert before is not None and before <= (written.time, written.inc)
    assert token_time({"_data": "zz"}) is None and token_time(None) is None


@pytest.mark.postgres
def test_a_database_is_handed_off_with_change_capture_by_the_control_plane(
        postgres_database, now, mongo_business, tmp_path):
    b = mongo_business
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + b["names"]["src"][-8:] * 4
    data["spec"]["data"]["protected_sets"] = list(COLLECTIONS)
    without_transport_control(data)  # this test's connections are not TLS
    h = prepared(postgres_database, now, tmp_path, data, engine="mongodb",
                 strategy="snapshot-cdc")
    route = Route(b["names"]["src"])
    settings = {
        "connect": lambda side: b["admin"][side],
        "connect_as": lambda side, writer: as_user(b, side, writer.rsplit("@", 1)[0]),
        "databases": {"source": b["names"]["src"], "target": b["names"]["tgt"]},
        "collections": COLLECTIONS, "writers": b["writers"],
        "app_user": f"{b['users']['app']}@admin", "admin_user": "sova-admin@admin",
        "invariants": {"ORDER-01": {"collection": "orders",
                                    "unique": ["customer", "idempotency_key"]}},
        "other_db": b["names"]["other"], "zone_of": lambda side: "approved-zone-a",
        "route": route, "scratch_db": f"scratch_{b['names']['src'][-8:]}",
        "catch_up_seconds": 60}
    worker = worker_for(h, mongodb_factory(h["adapter_id"], lambda t, a: settings,
                                           change_stream=True), owner="worker-mongodb-cdc")
    record = worker.drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert route.get() == b["names"]["tgt"]
    results = signed_results(h)
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    seen = h["observations"]
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith("availability.")}))


def test_an_update_too_large_for_one_event_arrives_whole(world):
    """A 9 MiB document updated: the event carries the document as it is now
    and the change, over the 16 MiB BSON limit, so it comes in fragments."""
    source = world.b["admin"]["source"][world.names["src"]]
    source.orders.insert_one({"_id": 900001, "customer": "large", "idempotency_key": "large-1",
                              "blob": "x" * (9 * 1024 * 1024)})
    for action in ("stage_target", "start_sync"):
        apply(world.adapter(), action)
    source.orders.update_one({"_id": 900001}, {"$set": {"blob": "y" * (9 * 1024 * 1024)}})
    window(world, ("apply_final_changes",))
    observation = world.adapter().readback(
        command("mongodb-handoff", "apply_final_changes"), datetime.now(UTC))
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    assert observation.evidence["brought_level_by"] == "change_stream"
    target = world.b["admin"]["target"][world.names["tgt"]].orders.find_one({"_id": 900001})
    assert target["blob"][:1] == "y" and len(target["blob"]) == 9 * 1024 * 1024
