"""The reference adapters against a real AWS account (opt-in, run-scoped).

* Object storage: a bucket in AWS S3 (the cloud source) handed off to a
  bucket on the local S3-compatible store (the on-premises target). Writers
  are IAM roles on AWS and gateway accounts on premises; the same logical
  writer is fenced on both through each store's own principals.
* The CloudTrail change collector, waiting for a real policy change to reach
  the event history.
* The authority witness on AWS S3 Object Lock (governance mode, so the run
  can clean up after itself; compliance mode would hold the objects for the
  whole retention).

Each test creates its own buckets named ``sova-test-...-<run>``; a run-scoped
cleanup removes them. See ``aws_fixtures`` for the opt-in variables.
"""

from datetime import UTC, datetime, timedelta
import json
import time
from uuid import uuid4

from adapter_conformance import HandoffAdapterConformance
from botocore.config import Config
from botocore.exceptions import ClientError
import pytest
from s3_world import S3World

from sova.adapters.aws_cloudtrail_collector import AwsCloudTrailCollector
from sova.adapters.s3_authority_witness import S3ObjectLockAuthorityWitness
from sova.domain.change_impact import ChangeKind
from sova.domain.contracts import ContractError

pytestmark = pytest.mark.aws_live
HOUR = timedelta(hours=1)


def _create(aws, client, bucket, **extra):
    client.create_bucket(Bucket=bucket, CreateBucketConfiguration={
        "LocationConstraint": aws["region"]}, **extra)
    client.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": aws["tags"]})


@pytest.fixture(scope="module")
def cloud_to_onprem(aws, aws_writer_roles, s3_stores):
    """Stores for S3World: source on AWS S3, target on the local gateway."""
    config = Config(retries={"max_attempts": 3}, s3={"addressing_style": "virtual"})
    admin = aws["session"].client("s3", config=config)

    def role_client(writer):
        c = aws_writer_roles["credentials"][writer]
        return aws["session"].client(
            "s3", aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
            aws_session_token=c["SessionToken"], config=config)

    writers = {"app": role_client("app"), "other": role_client("batch")}
    return {
        "root": lambda side: admin if side == "source" else s3_stores["root"]("target"),
        "as": lambda side, writer: (writers[writer] if side == "source"
                                    else s3_stores["as"]("target", writer)),
        "principals": {
            "source": {"app": aws_writer_roles["arn"]["app"],
                       "other": aws_writer_roles["arn"]["batch"]},
            "target": s3_stores["principals"]["target"]},
        "bucket_name": lambda side, suffix: (
            f"sova-test-src-{aws['run']}-{suffix}" if side == "source"
            else f"orders-tgt-{suffix}"),
        "create_bucket": lambda client, bucket: _create(aws, client, bucket),
    }


class TestAwsS3ToOnPremConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, cloud_to_onprem):
        return S3World(cloud_to_onprem)


@pytest.fixture
def locked_bucket(aws):
    client = aws["session"].client("s3")
    bucket = f"sova-test-witness-{aws['run']}-{uuid4().hex[:8]}"
    _create(aws, client, bucket, ObjectLockEnabledForBucket=True)
    return client, bucket


def test_the_witness_holds_on_aws_object_lock(locked_bucket):
    client, bucket = locked_bucket
    w = S3ObjectLockAuthorityWitness(client, bucket, tenant_id="tenant-a", retention=HOUR,
                                     mode="GOVERNANCE")
    w.require_locked_bucket()
    for revision in (1, 2, 3):
        w.append("orders", "a" * 64, "transition", {"revision": revision})
    assert w.latest("orders", "a" * 64) == {"revision": 3}
    versions = client.list_object_versions(Bucket=bucket)["Versions"]
    first = min(versions, key=lambda v: v["Key"])
    with pytest.raises(ClientError, match="AccessDenied"):  # locked: no bypass asked
        client.delete_object(Bucket=bucket, Key=first["Key"], VersionId=first["VersionId"])
    with pytest.raises(ClientError, match="AccessDenied"):
        client.put_object_retention(
            Bucket=bucket, Key=first["Key"], VersionId=first["VersionId"],
            Retention={"Mode": "GOVERNANCE",
                       "RetainUntilDate": datetime.now(UTC) + timedelta(minutes=1)})
    latest = max(versions, key=lambda v: v["Key"])["Key"]
    client.delete_object(Bucket=bucket, Key=latest)  # a delete marker hides it
    with pytest.raises(ContractError, match="hidden"):
        w.latest("orders", "a" * 64)


def test_compliance_mode_holds_even_against_the_bucket_owner(locked_bucket):
    """The production mode. The owner cannot bypass, shorten or downgrade it; the
    entries (and so the bucket) stay for the whole retention, here one hour."""
    client, bucket = locked_bucket
    w = S3ObjectLockAuthorityWitness(client, bucket, tenant_id="tenant-a", retention=HOUR,
                                     mode="COMPLIANCE")
    w.append("orders", "b" * 64, "opened", {"revision": 1})
    entry = client.list_object_versions(Bucket=bucket)["Versions"][0]
    version = {"Bucket": bucket, "Key": entry["Key"], "VersionId": entry["VersionId"]}
    assert client.head_object(**version)["ObjectLockMode"] == "COMPLIANCE"
    with pytest.raises(ClientError, match="AccessDenied"):
        client.delete_object(**version, BypassGovernanceRetention=True)
    for mode, until in (("COMPLIANCE", datetime.now(UTC) + timedelta(minutes=1)),
                        ("GOVERNANCE", datetime.now(UTC) + 2 * HOUR)):
        with pytest.raises(ClientError, match="AccessDenied"):
            client.put_object_retention(**version, BypassGovernanceRetention=True,
                                        Retention={"Mode": mode, "RetainUntilDate": until})
    assert w.latest("orders", "b" * 64) == {"revision": 1}


def test_the_witness_refuses_a_bucket_without_object_lock(aws):
    client = aws["session"].client("s3")
    bucket = f"sova-test-plain-{aws['run']}-{uuid4().hex[:8]}"
    _create(aws, client, bucket)
    with pytest.raises(ContractError, match="without Object Lock"):
        S3ObjectLockAuthorityWitness(client, bucket, tenant_id="tenant-a", retention=HOUR,
                                     mode="GOVERNANCE").require_locked_bucket()


class RecordingFeed:
    def __init__(self):
        self.recorded, self.advanced = [], []

    def record(self, event, *, collector_id):
        self.recorded.append(event)

    def advance(self, environment, *, collector_id, observed_through):
        self.advanced.append(observed_through)


def test_cloudtrail_collector_records_a_real_policy_change(aws):
    client, trail = aws["session"].client("s3"), aws["session"].client("cloudtrail")
    bucket = f"sova-test-trail-{aws['run']}-{uuid4().hex[:8]}"
    _create(aws, client, bucket)
    changed = datetime.now(UTC)
    client.put_bucket_policy(Bucket=bucket, Policy=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Sid": "ChangedOutsideThePlan", "Effect": "Deny",
            "Principal": {"AWS": aws["caller"]}, "Action": "s3:GetObjectAcl",
            "Resource": f"arn:aws:s3:::{bucket}/*"}]}))
    feed = RecordingFeed()
    collector = AwsCloudTrailCollector(
        feed, cloudtrail=trail, clock=lambda: datetime.now(UTC), tenant_id="tenant-a",
        application_id="orders", collector_id="cloudtrail-live",
        resources={bucket: ("aws-source", "uid-orders-bucket")},
        lookback=timedelta(minutes=30))
    found, deadline = [], time.monotonic() + 20 * 60
    while time.monotonic() < deadline:
        found += collector.poll()
        polled_at = datetime.now(UTC)  # after the poll: its clock read is earlier
        if any(e.kind == ChangeKind.POLICY for e in found):
            break
        time.sleep(30)
    policy = [e for e in found if e.kind == ChangeKind.POLICY]
    assert policy and policy[0].subject == "uid-orders-bucket"
    assert abs(policy[0].observed_at - changed) < timedelta(minutes=2)
    assert ChangeKind.RESOURCE in {e.kind for e in found}  # CreateBucket
    assert len({e.event_id for e in feed.recorded}) == len(feed.recorded)  # once each
    assert all(mark <= polled_at - timedelta(minutes=15) for mark in feed.advanced)
    print(f"policy change visible after {(polled_at - changed).total_seconds():.0f}s")
