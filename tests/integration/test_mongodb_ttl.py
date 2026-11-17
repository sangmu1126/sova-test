"""Documents a TTL index removes do not decide a MongoDB comparison: each server
removes expired documents on its own schedule (every 60 seconds), so right
after a copy one side may still hold sessions the other has already let go
(an Express or Django session store keeps sessions this way)."""

from datetime import UTC, datetime, timedelta

from adapter_conformance import apply, command
import pytest
from mongodb_business import as_user
from mongodb_world import COLLECTIONS, MongoWorld

from sova.adapters.mongodb_handoff import MongoDatabaseHandoffAdapter
from sova.adapters.mongodb_probes import MongoDatabaseProbes
from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.mongodb, pytest.mark.kubernetes]
UNIT = (*COLLECTIONS, "sessions")


def test_documents_a_ttl_index_removes_do_not_decide_the_comparison(mongo_business):
    b = mongo_business
    world = MongoWorld(b)
    source = b["admin"]["source"][b["names"]["src"]]
    source.sessions.create_index("expires_at", expireAfterSeconds=0, name="session_ttl")
    now = datetime.now(UTC)
    source.sessions.insert_many(
        [{"_id": f"gone-{i}", "expires_at": now - timedelta(minutes=10)} for i in range(5)] +
        [{"_id": f"live-{i}", "expires_at": now + timedelta(hours=1)} for i in range(5)])
    adapter = MongoDatabaseHandoffAdapter(
        "mongodb-handoff", connect=lambda side: b["admin"][side],
        source_db=b["names"]["src"], target_db=b["names"]["tgt"], collections=UNIT,
        writers=b["writers"], app_user=f"{b['users']['app']}@admin",
        admin_user="sova-admin@admin", route=world._route)
    for action in ("stage_target", "pause_admission", "fence_source_writes", "copy_content"):
        apply(adapter, action)
    target = b["admin"]["target"][b["names"]["tgt"]]
    target.sessions.delete_many({"_id": {"$regex": "^gone-"}})  # its TTL monitor ran first

    def compared():
        return adapter.readback(command("mongodb-handoff", "copy_content"), datetime.now(UTC))

    observation = compared()
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    sessions = observation.evidence["source"]["sessions"]
    assert sessions["documents"] == 5 and sessions["ttl_expiring"] in (0, 5), sessions
    probes = MongoDatabaseProbes(
        connect=lambda side: b["admin"][side],
        connect_as=lambda side, writer: as_user(b, side, writer.rsplit("@", 1)[0]),
        databases={"source": b["names"]["src"], "target": b["names"]["tgt"]},
        collections=UNIT, writers=b["writers"], app_user=f"{b['users']['app']}@admin")
    assert probes.content_equal("sessions")[0]
    target.sessions.update_one({"_id": "live-0"}, {"$set": {"user": "someone else"}})
    assert compared().status == EffectStatus.UNKNOWN  # a live session still counts
    assert not probes.content_equal("sessions")[0]
