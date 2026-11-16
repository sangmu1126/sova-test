"""The MongoDB adapter against the provider-neutral conformance kit, and what
MongoDB adds: no account lock (a login restricted to nowhere instead), a
connection that keeps its rights until the fence ends it, and indexes that
must arrive with the documents."""

from adapter_conformance import HandoffAdapterConformance, apply, readback
from mongodb_business import as_user
from mongodb_world import MongoWorld
import pytest

from sova.domain.contracts import EffectStatus

pytestmark = [pytest.mark.mongodb, pytest.mark.kubernetes]
HANDOFF = ("stage_target", "pause_admission", "fence_source_writes", "copy_content",
           "verify_final_state", "activate_target", "commit_route")


@pytest.fixture
def world(mongo_business):
    return MongoWorld(mongo_business)


class TestMongoDatabaseConformance(HandoffAdapterConformance):
    pass


def test_the_application_finds_its_documents_and_unique_index_on_the_target(world):
    for action in HANDOFF:
        apply(world.adapter(), action)
    errors = world.b["pymongo"].errors
    client = as_user(world.b, "target", world.users["app"])
    orders = client[world.names["tgt"]].orders
    assert orders.count_documents({}) == 40 and orders.find_one({"_id": 7})["amount"] == 70
    with pytest.raises(errors.DuplicateKeyError):  # the unique index came along
        orders.insert_one({"_id": 999, "customer": "customer-1", "idempotency_key": "key-1"})
    client.close()


def test_a_connection_opened_before_the_pause_loses_its_rights_at_once(world):
    """MongoDB re-checks a changed user on connections already open: restricting
    the login at the pause leaves an open connection unauthenticated at its
    next command - stricter than an account lock elsewhere."""
    errors = world.b["pymongo"].errors
    early = as_user(world.b, "source", world.users["app"])
    early[world.names["src"]].orders.find_one()  # logged in before the pause
    apply(world.adapter(), "pause_admission")
    with pytest.raises(errors.OperationFailure):
        early[world.names["src"]].orders.insert_one({"_id": 5000, "customer": "late",
                                                     "idempotency_key": "late-1"})
    apply(world.adapter(), "fence_source_writes")
    assert readback(world.adapter(), "fence_source_writes") == EffectStatus.APPLIED
    early.close()
