"""Change collector for AWS resources from CloudTrail event history (INV-08, §23.3).

Each poll looks up the management events of every registered resource and
records one change event per successful write call: a ``policy`` change for
calls that change who may do what, a ``resource`` change for calls that
change the resource itself, and ``unclassified`` for any other write, never
nothing. Read-only calls and calls AWS refused are not changes.

CloudTrail puts an event into the event history some minutes after the call
(typically within five, documented up to fifteen). A watermark claims that
every change up to its time has been recorded, so it never moves past
``now - delivery_lag``; each poll looks again from the watermark, so a late
event is still recorded (once: event IDs are stable and recording is
idempotent). On start the collector resumes from the feed's own watermark.

Event history keeps 90 days of management events of one region and nothing
of data events (object writes), which are not catalogue changes anyway. A
trail with organisation-wide delivery is the production source; the lookup
API is what an account offers without one, at two requests per second.
"""

from datetime import timedelta
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
        if not all(callable(getattr(feed, n, None)) for n in ("record", "advance")):
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
        self.seen = {}  # event ID -> event time, for events after the watermark

    def _since(self, environment, now):
        if environment not in self.marks:
            existing = ()
            if callable(getattr(self.feed, "watermarks", None)):
                existing = self.feed.watermarks((environment,))
            found = [m.observed_through for m in existing if m.environment_id == environment]
            self.marks[environment] = max(found) if found else now - self.lookback
        return self.marks[environment]

    def poll(self, *, cause=None):
        """Record the write calls seen since each environment's watermark."""
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
                    if event["EventId"] in self.seen:
                        continue  # looked at again because it is after the watermark
                    change = ChangeEvent(
                        f"{self.collector_id}:cloudtrail:{event['EventId']}", self.tenant_id,
                        self.application_id, classify(event["EventName"]), environment, uid,
                        None, event["EventTime"], cause)
                    self.feed.record(change, collector_id=self.collector_id)
                    self.seen[event["EventId"]] = change.observed_at
                    recorded.append(change)
        through = now - self.delivery_lag
        for environment in sorted({e for e, _ in self.resources.values()}):
            if through > self.marks[environment]:
                self.feed.advance(environment, collector_id=self.collector_id,
                                  observed_through=through)
                self.marks[environment] = through
        oldest = min(self.marks.values())
        self.seen = {i: t for i, t in self.seen.items() if t >= oldest}
        return tuple(recorded)
