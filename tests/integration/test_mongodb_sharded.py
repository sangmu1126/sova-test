"""A sharded MongoDB cluster as the source (two shards behind a mongos, the
results spread over both by a hashed key) brought onto a replica set: the
conformance kit holds for the copy, and for change capture through the
mongos's change stream - writes made on both shards while the copy runs
included. And the other way round: a replica set onto the sharded cluster,
its results sharded there before the handoff (the shard key's own index is
placement, not business, and is left out of comparisons)."""

from datetime import UTC, datetime
import threading
import time

from adapter_conformance import HandoffAdapterConformance, apply, command
import pytest
from mongodb_business import as_user
from mongodb_world import COLLECTIONS, MongoWorld

from sova.adapters.mongodb_change_stream import MongoChangeStreamHandoffAdapter
from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.mongodb, pytest.mark.kubernetes]


class ChangeStreamWorld(MongoWorld):
    def _adapter(self, source, target):
        clients = {"source": self.b["admin"][source], "target": self.b["admin"][target]}
        names = {"source": self._db(source), "target": self._db(target)}
        return MongoChangeStreamHandoffAdapter(
            "mongodb-handoff", connect=lambda side: clients[side], source_db=names["source"],
            target_db=names["target"], collections=COLLECTIONS, writers=self.b["writers"],
            app_user=f"{self.users['app']}@admin", admin_user="sova-admin@admin",
            route=self._route, catch_up_seconds=90)


@pytest.fixture
def world(mongo_sharded_business):
    return MongoWorld(mongo_sharded_business)


class TestShardedSourceConformance(HandoffAdapterConformance):
    pass


class TestShardedTargetConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, mongo_sharded_target_business):
        return MongoWorld(mongo_sharded_target_business)


class TestShardedChangeCaptureConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, mongo_sharded_business):
        return ChangeStreamWorld(mongo_sharded_business)


def test_writes_on_both_shards_while_the_copy_runs_reach_the_target(mongo_sharded_business):
    b = mongo_sharded_business
    world = ChangeStreamWorld(b)
    apply(world.adapter(), "stage_target")
    stop, done = threading.Event(), {"writes": 0}

    def business():  # the results are sharded: these land on both shards
        app = as_user(b, "source", b["users"]["app"])[b["names"]["src"]]
        i = 0
        while not stop.is_set():
            app.idempotency_results.insert_one({"_id": f"live:{i}", "result": "pending"})
            app.idempotency_results.replace_one({"_id": f"live:{max(i - 1, 0)}"},
                                                {"result": "confirmed"})
            app.idempotency_results.delete_one({"_id": f"customer-{i % 7}:key-{1 + i % 40}"})
            app.orders.update_one({"_id": 1 + i % 40}, {"$inc": {"amount": 1}})
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
    for action in ("pause_admission", "fence_source_writes", "apply_final_changes"):
        apply(world.adapter(), action)
    observation = world.adapter().readback(
        command("mongodb-handoff", "apply_final_changes"), datetime.now(UTC))
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    assert observation.evidence["brought_level_by"] == "change_stream"
    assert world.content("source") == world.content("target")
    print(f"writes while syncing: {done['writes']}")
