"""CloudTrail collector logic against a stub event history (no AWS)."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from sova.adapters.aws_cloudtrail_collector import AwsCloudTrailCollector, classify
from sova.domain.change_impact import ChangeKind, FeedWatermark
from sova.domain.contracts import ContractError

T0 = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
LAG = timedelta(minutes=15)


class History:
    """Event history whose events become visible only after a delivery delay."""

    def __init__(self):
        self.events, self.calls = [], []

    def add(self, name, resource, at, *, visible_at=None, read_only=False, error=None,
            event_id=None):
        self.events.append({
            "EventId": event_id or f"e-{len(self.events)}", "EventName": name,
            "EventTime": at, "ReadOnly": "true" if read_only else "false",
            "Resources": [{"ResourceName": resource}], "visible_at": visible_at or at,
            "CloudTrailEvent": json.dumps({"errorCode": error} if error else {})})

    def get_paginator(self, operation):
        assert operation == "lookup_events"
        return self

    def paginate(self, *, LookupAttributes, StartTime, EndTime):
        name = LookupAttributes[0]["AttributeValue"]
        self.calls.append((name, StartTime, EndTime))
        yield {"Events": [e for e in self.events
                          if e["Resources"][0]["ResourceName"] == name
                          and StartTime <= e["EventTime"] <= EndTime
                          and e["visible_at"] <= EndTime]}


class Feed:
    def __init__(self, marks=()):
        self.recorded, self.advanced, self.marks = [], [], list(marks)

    def record(self, event, *, collector_id):
        self.recorded.append(event)

    def advance(self, environment, *, collector_id, observed_through):
        self.advanced.append((environment, observed_through))

    def watermarks(self, environments):
        return tuple(m for m in self.marks if m.environment_id in environments)


def collector(history, feed, now):
    return AwsCloudTrailCollector(
        feed, cloudtrail=history, clock=lambda: now[0], tenant_id="tenant-a",
        application_id="orders", collector_id="cloudtrail-1", delivery_lag=LAG,
        resources={"orders-bucket": ("aws-source", "uid-bucket"),
                   "orders-db": ("aws-source", "uid-db")})


def test_write_calls_are_classified_and_refused_or_read_calls_are_not_changes():
    history, feed, now = History(), Feed(), [T0]
    history.add("PutBucketPolicy", "orders-bucket", T0 - timedelta(minutes=30))
    history.add("CreateBucket", "orders-bucket", T0 - timedelta(minutes=29))
    history.add("ModifyDBInstance", "orders-db", T0 - timedelta(minutes=28))
    history.add("GetBucketPolicy", "orders-bucket", T0 - timedelta(minutes=27), read_only=True)
    history.add("PutBucketPolicy", "orders-bucket", T0 - timedelta(minutes=26),
                error="AccessDenied")
    history.add("PutBucketPolicy", "someone-elses-bucket", T0 - timedelta(minutes=25))
    recorded = collector(history, feed, now).poll()
    assert [(e.kind, e.subject) for e in recorded] == [
        (ChangeKind.POLICY, "uid-bucket"), (ChangeKind.RESOURCE, "uid-bucket"),
        (ChangeKind.UNCLASSIFIED, "uid-db")]
    assert {name for name, _, _ in history.calls} == {"orders-bucket", "orders-db"}
    assert classify("SomethingNew") == ChangeKind.UNCLASSIFIED


def test_the_watermark_trails_by_the_delivery_lag_and_late_events_are_still_caught():
    history, feed, now = History(), Feed(), [T0]
    c = collector(history, feed, now)
    late_call = T0 - timedelta(minutes=5)
    history.add("PutBucketPolicy", "orders-bucket", late_call,
                visible_at=T0 + timedelta(minutes=8))  # delivered 13 minutes after the call
    assert c.poll() == ()
    assert feed.advanced == [("aws-source", T0 - LAG)]  # never claims the last 15 minutes
    now[0] = T0 + timedelta(minutes=10)
    assert [e.observed_at for e in c.poll()] == [late_call]
    now[0] = T0 + timedelta(minutes=20)
    assert c.poll() == ()  # found once, never again
    assert len(feed.recorded) == 1
    assert [t for _, t in feed.advanced] == sorted(t for _, t in feed.advanced)


def test_a_restarted_collector_resumes_from_the_feeds_watermark():
    history, now = History(), [T0]
    mark = T0 - timedelta(hours=3)
    feed = Feed([FeedWatermark("aws-source", mark)])
    history.add("PutBucketPolicy", "orders-bucket", T0 - timedelta(hours=2))
    assert len(collector(history, feed, now).poll()) == 1
    assert all(start == mark for _, start, _ in history.calls)


def test_an_unbounded_or_empty_registration_is_refused():
    history, feed = History(), Feed()
    with pytest.raises(ContractError):
        AwsCloudTrailCollector(feed, cloudtrail=history, clock=lambda: T0, tenant_id="t",
                               application_id="a", collector_id="c", resources={})
    with pytest.raises(ContractError):
        AwsCloudTrailCollector(feed, cloudtrail=history, clock=lambda: T0, tenant_id="t",
                               application_id="a", collector_id="c",
                               resources={"b": ("env", "uid")}, delivery_lag=timedelta(0))
