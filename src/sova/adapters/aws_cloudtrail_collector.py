"""Partial change observations; never an environment-completeness barrier.

CloudTrail management-event lookup has delayed delivery and limited registered-resource coverage. A delay allowance is not a delivery-completeness guarantee.
This collector records observed events, but never advances a change-feed
watermark. Admission needs a separately qualified, authenticated coverage
barrier for every relevant change channel. Late events remain useful for
invalidation; absent events never mean that the environment was unchanged.
"""

from datetime import timedelta
import hashlib
import json

from sova.domain.change_impact import ChangeEvent, ChangeKind
from sova.domain.contracts import ContractError, identifier, utc

POLICY_CALLS = frozenset({
    "PutBucketPolicy", "DeleteBucketPolicy", "PutBucketAcl", "PutPublicAccessBlock",
    "DeletePublicAccessBlock", "PutBucketOwnershipControls", "DeleteBucketOwnershipControls",
    "AuthorizeSecurityGroupIngress", "RevokeSecurityGroupIngress",
    "AuthorizeSecurityGroupEgress", "RevokeSecurityGroupEgress",
    "ModifySecurityGroupRules", "AddRoleToDBInstance", "RemoveRoleFromDBInstance",
})
RESOURCE_CALLS = frozenset({
    "CreateBucket", "DeleteBucket", "PutBucketVersioning", "PutBucketEncryption",
    "DeleteBucketEncryption", "PutObjectLockConfiguration", "PutBucketLifecycle",
    "PutBucketLifecycleConfiguration", "DeleteBucketLifecycle", "PutBucketReplication",
    "DeleteBucketReplication", "CreateDBInstance", "DeleteDBInstance", "RebootDBInstance",
    "StartDBInstance", "StopDBInstance", "RestoreDBInstanceFromDBSnapshot",
    "RestoreDBInstanceToPointInTime", "PromoteReadReplica",
})


def classify(event_name):
    if event_name in POLICY_CALLS:
        return ChangeKind.POLICY
    if event_name in RESOURCE_CALLS:
        return ChangeKind.RESOURCE
    return ChangeKind.UNCLASSIFIED  # e.g. ModifyDBInstance: class, network or roles alike


class AwsCloudTrailCollector:
    def __init__(self, feed, *, cloudtrail, clock, tenant_id, application_id, collector_id,
                 resources, delivery_lag=timedelta(minutes=15), lookback=timedelta(hours=1)):
        """``resources`` maps a CloudTrail resource name (bucket name, DB
        instance identifier) to its (environment, resource UID)."""
        if not all(callable(getattr(feed, n, None)) for n in ("record",)):
            raise ContractError("Change feed required")
        if not callable(getattr(cloudtrail, "get_paginator", None)) or not callable(clock):
            raise ContractError("CloudTrail client and trusted clock required")
        if type(resources) is not dict or not resources:
            raise ContractError("Resources mapped to (environment, resource UID) required")
        for name, (environment, uid) in resources.items():
            identifier(name, "resource name", limit=2048)
            identifier(environment, "environment_id")
            identifier(uid, "resource UID")
        if not timedelta(0) < delivery_lag <= timedelta(hours=24):
            raise ContractError("Bounded positive delivery lag required")
        self.feed, self.cloudtrail, self.clock = feed, cloudtrail, clock
        self.tenant_id = identifier(tenant_id, "tenant_id")
        self.application_id = identifier(application_id)
        self.collector_id = identifier(collector_id, "collector_id")
        self.resources = dict(resources)
        self.delivery_lag, self.lookback = delivery_lag, lookback
        self.marks = {}
        # One provider event may affect multiple registered resources, even in
        # different environments. Dedupe the binding, never the provider ID alone.
        self.seen = {}

    def _since(self, environment, now):
        if environment not in self.marks:
            existing = ()
            if callable(getattr(self.feed, "watermarks", None)):
                existing = self.feed.watermarks((environment,))
            found = [m.observed_through for m in existing if m.environment_id == environment]
            self.marks[environment] = max(found) if found else now - self.lookback
        return self.marks[environment]

    def poll(self, *, cause=None):
        """Record observed writes; do not certify coverage or advance a barrier."""
        now = utc(self.clock())
        recorded = []
        for name, (environment, uid) in sorted(self.resources.items()):
            pages = self.cloudtrail.get_paginator("lookup_events").paginate(
                LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": name}],
                StartTime=self._since(environment, now), EndTime=now)
            for page in pages:
                for event in page.get("Events", ()):
                    if event.get("ReadOnly") == "true":
                        continue
                    if json.loads(event.get("CloudTrailEvent") or "{}").get("errorCode"):
                        continue  # AWS refused the call: nothing changed
                    binding = (event["EventId"], environment, uid)
                    binding_digest = hashlib.sha256(json.dumps(
                        binding, separators=(",", ":"), ensure_ascii=True,
                    ).encode()).hexdigest()
                    change = ChangeEvent(
                        f"{self.collector_id}:cloudtrail-v2:{binding_digest}", self.tenant_id,
                        self.application_id, classify(event["EventName"]), environment, uid,
                        None, event["EventTime"], cause)
                    if binding in self.seen:
                        if self.seen[binding] != change:
                            raise ContractError("CloudTrail event binding changed on replay")
                        continue
                    if len(self.seen) >= 10000:
                        raise ContractError("Partial observation history exceeds its bound; coverage remains unknown")
                    inserted = self.feed.record(change, collector_id=self.collector_id)
                    self.seen[binding] = change
                    if inserted is not False:
                        recorded.append(change)
        # Observation progress cannot certify that every earlier change arrived.
        return tuple(recorded)
