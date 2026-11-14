"""Object-storage offline copy between two independent S3-compatible stores.

The source and target are separate stores (separate accounts, storage and
access policies), reached through separate clients, as two providers would
be. Writers are real accounts on each store; their access is decided by the
stores' own bucket-policy evaluation. Write probes are conditional writes that
cannot succeed: an allowed writer is told the object does not exist, a denied
one is refused, and nothing is ever written. No cloud resource is touched.
"""

import hashlib

from adapter_conformance import HandoffAdapterConformance, SEQUENCES, apply, readback
from botocore.exceptions import ClientError
import pytest
from s3_world import LARGE, S3World, canonical

from sova.adapters.s3_bucket_policy_access import S3BucketPolicyAccess
from sova.adapters.s3_offline_copy import S3OfflineCopyAdapter
from sova.domain.contracts import ContractError, EffectStatus

pytestmark = pytest.mark.s3


@pytest.fixture
def world(s3_stores):
    return S3World(s3_stores)


class TestS3OfflineCopyConformance(HandoffAdapterConformance):
    pass  # the module-level ``world`` fixture supplies the S3 world


def run(world, through="commit_route", strategy="offline-copy"):
    steps = SEQUENCES[strategy]
    for action in steps[:steps.index(through) + 1]:
        apply(world.adapter(), action)


def test_the_customers_own_policy_survives_the_handoff(world):
    run(world)
    statements = world.statements("source")
    assert statements["ApplicationAccess"] == canonical(world.base_policy["Statement"][0])
    assert {"SovaHandoffPauseAdmission", "SovaHandoffFenceWrites"} <= set(statements)
    target = world.statements("target")
    principal = world.principals["target"]
    assert target["SovaHandoffWriter"]["Principal"]["AWS"] in (
        [principal["app"]], principal["app"])
    assert target["SovaHandoffTargetNoAccess"]["Principal"]["AWS"] in (
        [principal["other"]], principal["other"])


def test_large_objects_tags_and_metadata_arrive_intact(world):
    run(world)
    target, bucket = world.root["target"], world.buckets["target"]
    large = target.get_object(Bucket=bucket, Key="exports/large.bin")
    assert large["ContentLength"] == LARGE
    assert hashlib.sha256(large["Body"].read()).hexdigest() == \
        hashlib.sha256(b"x" * LARGE).hexdigest()
    assert target.get_object_tagging(Bucket=bucket, Key="orders/0004.json")["TagSet"] == [
        {"Key": "class", "Value": "invoice"}]
    order = target.get_object(Bucket=bucket, Key="orders/0002.json")
    assert order["ContentType"] == "application/json"
    assert order["Metadata"] == {"customer": "c-2"}


def test_objects_removed_on_the_source_are_removed_on_the_target(world):
    run(world, "start_sync", "snapshot-cdc")
    world.s["as"]("source", "app").delete_object(Bucket=world.buckets["source"],
                                                 Key="orders/0003.json")
    run(world, "commit_route", "snapshot-cdc")
    with pytest.raises(ClientError):
        world.root["target"].head_object(Bucket=world.buckets["target"],
                                         Key="orders/0003.json")
    assert world.content("source") == world.content("target")


def test_version_history_a_current_copy_would_lose_is_not_verified(s3_stores):
    world = S3World(s3_stores, versioned=True)
    app = s3_stores["as"]("source", "app")
    app.put_object(Bucket=world.buckets["source"], Key="orders/0001.json", Body=b"v2")
    run(world, "copy_content")
    # The target was created versioned like the source, and the bytes match.
    assert world.root["target"].get_bucket_versioning(
        Bucket=world.buckets["target"])["Status"] == "Enabled"
    assert world.content("source") == world.content("target")
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.UNKNOWN
    world.accept_history_loss = True  # an explicit, contract-level decision
    assert readback(world.adapter(), "verify_final_state") == EffectStatus.APPLIED


def test_a_writer_left_out_of_the_registration_is_not_fenced(s3_stores):
    """The probe tries every writer that really exists, as for PostgreSQL."""
    world = S3World(s3_stores)
    partial = S3OfflineCopyAdapter(
        "s3-offline-copy", source=world.side("source"), target=world.side("target"),
        app_principal="app", writer_principals=("app",), route=world._route)
    for action in ("stage_target", "pause_admission", "fence_source_writes"):
        apply(partial, action)
    assert readback(partial, "fence_source_writes") == EffectStatus.APPLIED
    assert world.can_write("source", "other")  # the batch account still writes


def test_unsupported_shapes_are_refused(s3_stores):
    world = S3World(s3_stores)
    with pytest.raises(ContractError):
        S3OfflineCopyAdapter("s3-offline-copy", source=("c", "b"), target=("c", "b", "a"),
                             app_principal="a", writer_principals=("a",), route=world._route)
    with pytest.raises(ContractError):  # the application must be a registered writer
        S3OfflineCopyAdapter("s3-offline-copy", source=("c", "b", "a"),
                             target=("c", "b", "a"), app_principal="a",
                             writer_principals=("b",), route=world._route)
    with pytest.raises(ContractError):  # policy statement names stay bounded
        S3BucketPolicyAccess(world.root["source"], world.buckets["source"]).deny(
            "bad name!", ("x",))
    with pytest.raises(ContractError):  # a writer this store has no principal for
        world.side("source")[2].denied(("nightly-job",))


def test_more_objects_than_the_bound_read_back_unknown_and_refuse_to_copy(s3_stores):
    world = S3World(s3_stores)
    apply(world.adapter(), "stage_target")

    bounded = S3OfflineCopyAdapter(
        "s3-offline-copy", source=world.side("source"), target=world.side("target"),
        app_principal="app", writer_principals=("app", "other"), route=world._route,
        max_objects=3)
    assert readback(bounded, "copy_content") == EffectStatus.UNKNOWN
    with pytest.raises(ContractError):
        apply(bounded, "copy_content")
    assert world.content("target") != world.content("source")  # nothing half-copied
