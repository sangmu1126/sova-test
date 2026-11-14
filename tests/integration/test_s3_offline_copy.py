"""Object-storage offline copy between two independent S3-compatible stores.

The source and target are separate stores (separate accounts, storage and
access policies), reached through separate clients, as two providers would
be. Writers are real accounts on each store; their access is decided by the
stores' own bucket-policy evaluation. Write probes are conditional writes that
cannot succeed: an allowed writer is told the object does not exist, a denied
one is refused, and nothing is ever written. No cloud resource is touched.
"""

import hashlib
import json
from uuid import uuid4

from adapter_conformance import HandoffAdapterConformance, SEQUENCES, apply, readback
from botocore.exceptions import ClientError
import pytest

from sova.adapters.s3_bucket_policy_access import S3BucketPolicyAccess
from sova.adapters.s3_offline_copy import S3OfflineCopyAdapter
from sova.domain.contracts import ContractError, EffectStatus

pytestmark = pytest.mark.s3
LARGE = 9 * 1024 * 1024  # above the default multipart threshold of the copy


class Route:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def application_access(bucket, principals):
    """The customer's own policy before any handoff: both writers may work."""
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "ApplicationAccess", "Effect": "Allow", "Principal": {"AWS": principals},
        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
                   "s3:GetObjectTagging", "s3:PutObjectTagging",
                   "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}]}


class S3World:
    def __init__(self, stores, *, versioned=False, accept_history_loss=False):
        self.s = stores
        suffix = uuid4().hex[:10]
        self.buckets = {"source": f"orders-src-{suffix}", "target": f"orders-tgt-{suffix}"}
        self.root = {side: stores["root"](side) for side in ("source", "target")}
        self.principals = stores["principal"]
        self.accept_history_loss = accept_history_loss
        source, bucket = self.root["source"], self.buckets["source"]
        source.create_bucket(Bucket=bucket)
        if versioned:
            source.put_bucket_versioning(Bucket=bucket,
                                         VersioningConfiguration={"Status": "Enabled"})
        self.base_policy = application_access(bucket, sorted(self.principals.values()))
        source.put_bucket_policy(Bucket=bucket, Policy=json.dumps(self.base_policy))
        app = stores["as"]("source", "app")
        for i in range(1, 11):
            app.put_object(Bucket=bucket, Key=f"orders/{i:04d}.json",
                           Body=json.dumps({"order": i, "amount": i * 10}).encode(),
                           ContentType="application/json", Metadata={"customer": f"c-{i % 3}"},
                           **({"Tagging": "class=invoice"} if i % 4 == 0 else {}))
        app.upload_fileobj(_Bytes(b"x" * LARGE), bucket, "exports/large.bin",
                           ExtraArgs={"ContentType": "application/octet-stream"})
        self.next_order = 11
        self._route = Route(self.route_of("source"))
        self.pending = None

    def _adapter(self, source, target):
        def side(name):
            client = self.s["root"](name)
            return client, self.buckets[name], S3BucketPolicyAccess(client, self.buckets[name])
        return S3OfflineCopyAdapter(
            "s3-offline-copy", source=side(source), target=side(target),
            app_principal=self.principals["app"],
            writer_principals=tuple(self.principals.values()), route=self._route,
            accept_history_loss=self.accept_history_loss)

    def adapter(self):
        return self._adapter("source", "target")

    def reverse_adapter(self):
        return self._adapter("target", "source")

    def _exists(self, side):
        try:
            self.root[side].head_bucket(Bucket=self.buckets[side])
            return True
        except ClientError:
            return False

    def can_write(self, side, writer):
        """A conditional write that can never succeed: If-Match with an ETag no
        object has, on a key that does not exist. An allowed writer gets "no
        such key" (or a failed precondition), a denied one an access denial;
        nothing is ever written."""
        if not self._exists(side):
            return False
        try:
            self.s["as"](side, writer).put_object(
                Bucket=self.buckets[side], Key="sova-write-probe", Body=b"",
                IfMatch='"00000000000000000000000000000000"')
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in ("NoSuchKey", "PreconditionFailed"):
                return True
            if code == "AccessDenied":
                return False
            raise
        raise AssertionError("a conditional probe wrote an object")

    def business_write(self, count):
        app = self.s["as"]("source", "app")
        for i in range(self.next_order, self.next_order + count):
            app.put_object(Bucket=self.buckets["source"], Key=f"orders/{i:04d}.json",
                           Body=json.dumps({"order": i}).encode(),
                           ContentType="application/json")
        self.next_order += count

    def business_delete(self, count):
        app, bucket = self.s["as"]("source", "app"), self.buckets["source"]
        listed = app.list_objects_v2(Bucket=bucket, Prefix="orders/", MaxKeys=count)
        for item in listed.get("Contents", ()):
            app.delete_object(Bucket=bucket, Key=item["Key"])

    def content(self, side):
        if not self._exists(side):
            return None
        client, bucket, h = self.root[side], self.buckets[side], hashlib.sha256()
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
        for key in sorted(o["Key"] for p in pages for o in p.get("Contents", ())):
            r = client.get_object(Bucket=bucket, Key=key)
            tags = client.get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
            h.update(json.dumps([key, hashlib.sha256(r["Body"].read()).hexdigest(),
                                 r.get("ContentType"), r.get("Metadata"),
                                 sorted((t["Key"], t["Value"]) for t in tags)]).encode())
        return h.hexdigest()

    def _policy(self, side):
        try:
            return json.loads(self.root[side].get_bucket_policy(
                Bucket=self.buckets[side])["Policy"])
        except ClientError as error:
            return error.response["Error"]["Code"]

    def _uploads(self, side):
        if not self._exists(side):
            return None
        return sorted(u["Key"] for u in self.root[side].list_multipart_uploads(
            Bucket=self.buckets[side]).get("Uploads", ()))

    def state(self):
        return tuple((self.content(side), json.dumps(self._policy(side), sort_keys=True),
                      self._uploads(side)) for side in ("source", "target")) + (
            self._route.get(),)

    def reopen_source(self):
        """The source policy restored from before the handoff."""
        self.root["source"].put_bucket_policy(Bucket=self.buckets["source"],
                                              Policy=json.dumps(self.base_policy))

    def tamper_target(self):
        self.root["target"].put_object(Bucket=self.buckets["target"],
                                       Key="orders/0001.json", Body=b'{"order": 1, "x": 1}',
                                       ContentType="application/json",
                                       Metadata={"customer": "c-1"})

    def move_route(self, side):
        self._route.set(self.route_of(side))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return f"{self.s['endpoints'][side]}/{self.buckets[side]}"

    def open_pending_write(self):
        app = self.s["as"]("source", "app")
        upload = app.create_multipart_upload(Bucket=self.buckets["source"],
                                             Key="exports/pending.bin")
        app.upload_part(Bucket=self.buckets["source"], Key="exports/pending.bin",
                        UploadId=upload["UploadId"], PartNumber=1, Body=b"p" * 1024)
        self.pending = upload["UploadId"]

    def resolve_pending_write(self):
        self.root["source"].abort_multipart_upload(Bucket=self.buckets["source"],
                                                   Key="exports/pending.bin",
                                                   UploadId=self.pending)


class _Bytes:
    """A minimal readable stream, so the seed upload itself goes multipart."""

    def __init__(self, data):
        self.data, self.at = data, 0

    def read(self, size=-1):
        end = len(self.data) if size is None or size < 0 else self.at + size
        chunk, self.at = self.data[self.at:end], min(end, len(self.data))
        return chunk


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
    statements = {s["Sid"]: s for s in world._policy("source")["Statement"]}
    assert statements["ApplicationAccess"] == world.base_policy["Statement"][0]
    assert {"SovaHandoffPauseAdmission", "SovaHandoffFenceWrites"} <= set(statements)
    target = {s["Sid"]: s for s in world._policy("target")["Statement"]}
    assert target["SovaHandoffWriter"]["Principal"] == {"AWS": [world.principals["app"]]}
    assert target["SovaHandoffTargetNoAccess"]["Principal"] == {
        "AWS": [world.principals["other"]]}


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
        "s3-offline-copy",
        source=(world.root["source"], world.buckets["source"],
                S3BucketPolicyAccess(world.root["source"], world.buckets["source"])),
        target=(world.root["target"], world.buckets["target"],
                S3BucketPolicyAccess(world.root["target"], world.buckets["target"])),
        app_principal=world.principals["app"], writer_principals=(world.principals["app"],),
        route=world._route)
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


def test_more_objects_than_the_bound_read_back_unknown_and_refuse_to_copy(s3_stores):
    world = S3World(s3_stores)
    apply(world.adapter(), "stage_target")

    def side(name):
        client = world.root[name]
        return client, world.buckets[name], S3BucketPolicyAccess(client, world.buckets[name])

    bounded = S3OfflineCopyAdapter(
        "s3-offline-copy", source=side("source"), target=side("target"),
        app_principal=world.principals["app"],
        writer_principals=tuple(world.principals.values()), route=world._route, max_objects=3)
    assert readback(bounded, "copy_content") == EffectStatus.UNKNOWN
    with pytest.raises(ContractError):
        apply(bounded, "copy_content")
    assert world.content("target") != world.content("source")  # nothing half-copied
