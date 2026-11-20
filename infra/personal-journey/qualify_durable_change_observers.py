"""Persist real partial observations; no complete-feed or migration claim.

Reads only the owned EC2/SG CloudTrail histories and changes a NEW source scratch
database. App tables, routes, grants and registrations are untouched. Appends
observations using the existing non-owner ledger runtime. Never calls advance().
The impact index is explicitly diagnostic, not a compiled or admitted plan.
"""

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from sova.adapters.aws_cloudtrail_collector import AwsCloudTrailCollector
from sova.adapters.kubernetes.https import private_text
from sova.adapters.postgres_change_collector import PostgresCatalogCollector
from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.domain.change_impact import ChangeKind, ImpactIndex, assess_impact


RUN = "b0be270e62"
SOURCE = "aws-ec2-" + RUN
TARGET = "local-rke2-" + RUN
INSTANCE = "i-04946f0e0c0548c3b"


class ObservationOnlyFeed:
    """The collector receives append capability, never watermark authority."""

    def __init__(self, feed):
        self._feed = feed

    def record(self, event, *, collector_id):
        return self._feed.record(event, collector_id=collector_id)


def diagnostic_impact(feed, *, tenant, app, since, scratch_uid):
    # This is not PlanRevision/LifecyclePlan and is never stored or admitted.
    index = ImpactIndex(
        hashlib.sha256(("diagnostic-only:" + scratch_uid).encode()).hexdigest(),
        tenant, app, since, ((SOURCE, "source"), (TARGET, "target")),
        frozenset({SOURCE, TARGET}), ((SOURCE, scratch_uid, "scratch-only"),),
        (), frozenset(), (),
        (("diagnostic.observation", frozenset({"source", "target"}),
          frozenset({"scratch-only"}), "final_content_comparison", "diagnostic-only"),),
    )
    events = feed.events(application_id=app, environments=(SOURCE, TARGET), since=since)
    marks = feed.watermarks((SOURCE, TARGET))
    return assess_impact(index, events, marks, issued=0, now=datetime.now(UTC))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "control_directory", "artifacts"):
        p.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
    p.add_argument("--collector-directory", type=Path, required=True,
                   help="Already-qualified append-only observer credential; host audit reads stay separate")
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    code_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    identity = json.loads((a.inputs / "identity.json").read_text())
    links = json.loads(a.connections.read_text())
    if identity["run_id"] != RUN or links["run_id"] != RUN or links["ready"] is not True or datetime.now(UTC) >= datetime.fromisoformat(identity["expires_at"]):
        raise ValueError("Unexpired connected owned trial required")
    cfg = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    if session.client("sts", config=cfg).get_caller_identity()["Account"] != "265233844540":
        raise ValueError("Personal account only")
    instance = session.client("ec2", config=cfg).describe_instances(InstanceIds=[INSTANCE])["Reservations"][0]["Instances"][0]
    if instance["State"]["Name"] != "running" or {t["Key"]: t["Value"] for t in instance["Tags"]}.get("RunId") != RUN:
        raise ValueError("Exact owned source required")
    env = identity["environments"]["source"]

    def source_connect(database="source_orders", *, autocommit=False):
        return psycopg.connect(make_conninfo(
            host=env["database_host"], hostaddr="127.0.0.1", port=links["ports"]["source_db"],
            user="postgres", password=private_text(a.inputs / "client/source-admin.password"),
            dbname=database, sslmode="verify-full", sslrootcert=str(a.inputs / "pki/ca.pem"),
            connect_timeout=5, options="-c statement_timeout=5000 -c lock_timeout=1000"), autocommit=autocommit)

    def original():
        with source_connect() as c:
            c.execute("SET TRANSACTION READ ONLY")
            if c.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0] != "7709720208587968539":
                raise ValueError("Wrong source database server")
            return list(c.execute("SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)").fetchone())

    dsn = private_text(a.control_directory / "runtime.dsn")
    observer_dsn = private_text(a.collector_directory / "observer.dsn")
    settings = conninfo_to_dict(observer_dsn)
    if settings.get("dbname") != "sj_control_" + RUN or settings.get("user") != "sj_observe_" + RUN or settings.get("sslmode") != "verify-full":
        raise ValueError("Exact already-qualified observation-only role required")

    def connect():
        return psycopg.connect(observer_dsn, connect_timeout=5, options="-c statement_timeout=5000 -c lock_timeout=1000")

    tenant, app = identity["tenant_id"], identity["application_id"]
    feed = PostgresChangeFeed(connect, tenant_id=tenant)
    audit_feed = PostgresChangeFeed(lambda: psycopg.connect(dsn, connect_timeout=5), tenant_id=tenant)

    def counts():
        with audit_feed.transaction() as c:
            row = c.execute("SELECT current_database(),current_user,(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid())").fetchone()
            if row != ("sj_control_" + RUN, "sj_runtime_" + RUN, True):
                raise ValueError("Wrong verified native runtime ledger")
            return [c.execute("SELECT count(*) FROM sova_control." + name + " WHERE application_id=%s", (app,)).fetchone()[0] for name in ("plans", "operations", "effects")]

    before, ledger_before = original(), counts()
    if before != [1, 1, 1] or ledger_before != [0, 0, 0]:
        raise ValueError("Existing original app work precludes this qualification")
    before_marks = feed.watermarks((SOURCE, TARGET))
    if before_marks:
        raise ValueError("External environment already has a barrier; do not alter its authority")
    started = datetime.now(UTC)
    database = "q_durable_feed_" + uuid4().hex[:12]
    report = {"version": 1, "started_at": started.isoformat(), "run_id": RUN,
              "scratch_database": database, "scratch_retained": True, "checks": {},
              "original_plan_admitted": False, "migration_complete": False,
              "complete_feed_qualified": False, "watermark_advanced": False,
              "collector_uses_dedicated_role": bool(a.collector_directory)}
    output = a.artifacts / "result.json"

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")

    save()
    try:
        with source_connect(autocommit=True) as c:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            c.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        with source_connect(database) as c:
            c.execute("CREATE TABLE observation_probe(id bigint PRIMARY KEY)")
            oid = c.execute("SELECT oid::text FROM pg_database WHERE datname=current_database()").fetchone()[0]
        scratch_uid = "pg-7709720208587968539-db-" + oid
        limited = ObservationOnlyFeed(feed)
        catalog = PostgresCatalogCollector(
            limited, connect=lambda db: source_connect(db), tenant_id=tenant,
            application_id=app, collector_id="personal-durable-catalog-v1",
            databases={database: (SOURCE, scratch_uid)})
        report["checks"]["initial_snapshot_is_not_a_change_or_barrier"] = catalog.poll() == ()
        with source_connect(database) as c:
            c.execute("ALTER TABLE observation_probe ADD COLUMN change_seen text")
        events = catalog.poll()
        report["checks"]["actual_scratch_schema_change_appended"] = len(events) == 1 and events[0].kind == ChangeKind.SCHEMA
        restarted = PostgresChangeFeed(connect, tenant_id=tenant)
        read = restarted.events(application_id=app, environments=(SOURCE,), since=started)
        report["checks"]["new_native_feed_reads_the_same_schema_event"] = all(e in read for e in events) and bool(events)
        if not events:
            raise ValueError("No actual catalog event was observed")
        report["checks"]["same_record_replay_is_idempotent"] = restarted.record(events[0], collector_id="personal-durable-catalog-v1") is False
        with feed.transaction() as c:
            try:
                with c.transaction():
                    c.execute("DELETE FROM sova_control.change_events WHERE event_id=%s", (events[0].event_id,))
            except psycopg.errors.InsufficientPrivilege:
                report["checks"]["runtime_history_delete_denied"] = True
            else:
                raise ValueError("Unexpected history deletion authority")
        other = PostgresChangeFeed(connect, tenant_id="other-" + uuid4().hex)
        report["checks"]["other_tenant_cannot_read_actual_observation"] = other.events(application_id=app, environments=(SOURCE,), since=started) == ()
        resources = {INSTANCE: (SOURCE, INSTANCE)}
        resources.update({item["GroupId"]: (SOURCE, item["GroupId"]) for item in instance["SecurityGroups"]})

        def aws_collector():
            return AwsCloudTrailCollector(
                ObservationOnlyFeed(PostgresChangeFeed(connect, tenant_id=tenant)),
                cloudtrail=session.client("cloudtrail", config=cfg), clock=lambda: datetime.now(UTC),
                tenant_id=tenant, application_id=app, collector_id="personal-durable-cloudtrail-v2",
                resources=resources, lookback=timedelta(hours=8))

        live_collector = aws_collector()
        cloud = live_collector.poll()
        # A previously committed observation is still an actual current AWS
        # lookup, but must never be reported as a new append after restart.
        observed_cloud = tuple(live_collector.seen.values())
        report["checks"]["actual_registered_aws_history_observed"] = bool(observed_cloud)
        replay = aws_collector().poll()
        report["checks"]["restarted_aws_collector_does_not_reemit_stored_bindings"] = not ({e.event_id for e in cloud} & {e.event_id for e in replay})
        actual = restarted.events(application_id=app, environments=(SOURCE,), since=started - timedelta(hours=8))
        report["checks"]["aws_events_persisted_after_collector_restart"] = all(e in actual for e in observed_cloud) and bool(observed_cloud)
        assessment = diagnostic_impact(restarted, tenant=tenant, app=app, since=started, scratch_uid=scratch_uid)
        report["diagnostic_impact"] = assessment.document
        report["diagnostic_is_not_an_admitted_or_compiled_plan"] = True
        report["checks"]["partial_observation_keeps_commands_blocked_unknown"] = assessment.plan_unknown and not assessment.commands_allowed
        report["checks"]["actual_schema_change_invalidates_diagnostic_structure"] = assessment.plan_stale
        after_marks = restarted.watermarks((SOURCE, TARGET))
        report["checks"]["no_environment_barrier_created"] = after_marks == before_marks == ()
        report["new_cloud_events"] = len(cloud)
        report["existing_cloud_events_read_from_actual_aws"] = len(observed_cloud) - len(cloud)
        report["events"] = [{"id": e.event_id, "kind": str(e.kind), "environment": e.environment_id, "subject": e.subject, "observed_at": e.observed_at.isoformat()} for e in (*events, *observed_cloud, *replay)]
        report["passed"] = all(report["checks"].values())
    finally:
        report["original_counts_after"] = original()
        report["original_counts_before"] = before
        report["original_ledger_counts_before"] = ledger_before
        report["original_ledger_counts_after"] = counts()
        report["original_counts_unchanged"] = report["original_counts_after"] == before
        report["original_plans_operations_effects_unchanged"] = report["original_ledger_counts_after"] == ledger_before
        report["completed_at"] = datetime.now(UTC).isoformat()
        report["driver_sha256"] = code_digest
        report["code_unchanged_during_trial"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == code_digest
        report["window_unexpired_at_completion"] = datetime.now(UTC) < datetime.fromisoformat(identity["expires_at"])
        save()
    print(json.dumps({"passed": report.get("passed", False), "checks": len(report["checks"]), "complete_feed_qualified": False, "migration_complete": False}))
    if not all((report.get("passed"), report["original_counts_unchanged"], report["original_plans_operations_effects_unchanged"], report["code_unchanged_during_trial"], report["window_unexpired_at_completion"])):
        raise ValueError("Durable observation qualification failed; raw record retained")


if __name__ == "__main__":
    main()
