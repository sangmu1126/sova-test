"""Actual partial catalog/audit observation, never migration admission.

Uses a new source-side scratch DB and only the registered owned EC2 history.
The existing app's rows remain identical. Snapshot polling deliberately does
not certify changes which were reverted between polls; no watermark advances.
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
from psycopg.conninfo import make_conninfo

from sova.adapters.aws_cloudtrail_collector import AwsCloudTrailCollector
from sova.adapters.postgres_change_collector import PostgresCatalogCollector
from sova.domain.change_impact import ChangeKind


class Observations:
    def __init__(self):
        self.events = []

    def record(self, event, *, collector_id):
        self.events.append(
            {
                "id": event.event_id,
                "kind": str(event.kind),
                "environment": event.environment_id,
                "subject": event.subject,
                "observed_at": event.observed_at.isoformat(),
            }
        )

    def advance(self, *args, **kwargs):
        raise ValueError("A partial observation cannot certify exhaustive coverage")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((a.inputs / "identity.json").read_text())
    links = json.loads(a.connections.read_text())
    if identity["run_id"] != "b0be270e62" or datetime.now(
        UTC
    ) >= datetime.fromisoformat(identity["expires_at"]):
        raise ValueError("Unexpired owned trial required")
    item = identity["environments"]["source"]

    def connect(database="source_orders", *, autocommit=False):
        return psycopg.connect(
            make_conninfo(
                host=item["database_host"],
                hostaddr="127.0.0.1",
                port=links["ports"]["source_db"],
                dbname=database,
                user="postgres",
                password=(a.inputs / "client/source-admin.password").read_text(),
                sslmode="verify-full",
                sslrootcert=str(a.inputs / "pki/ca.pem"),
                connect_timeout=5,
                options="-c statement_timeout=5000 -c lock_timeout=2000",
            ),
            autocommit=autocommit,
        )

    def original():
        with connect() as c:
            system = c.execute(
                "SELECT system_identifier::text FROM pg_control_system()"
            ).fetchone()[0]
            if system != "7709720208587968539":
                raise ValueError("Wrong original source")
            return {
                "system_identifier": system,
                "rows": list(
                    c.execute(
                        "SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)"
                    ).fetchone()
                ),
            }

    before = original()
    if before["rows"] != [1, 1, 1]:
        raise ValueError("Owned original application is no longer idle")
    database = "q_catalog_" + uuid4().hex[:12]
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "scratch_database": database,
        "checks": {},
        "migration_complete": False,
        "coverage_complete": False,
        "watermark_advanced": False,
        "scratch_retained": True,
    }
    try:
        with connect(autocommit=True) as c:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            c.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database)
                )
            )
            c.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(database),
                    sql.Literal("Owned b0be270e62 catalog observation only; not residency evidence"),
                ),
            )
        with connect(database, autocommit=True) as c:
            c.execute("CREATE TABLE observation_probe(id bigint PRIMARY KEY)")
        feed = Observations()
        collector = PostgresCatalogCollector(
            feed,
            connect=connect,
            tenant_id=identity["tenant_id"],
            application_id=identity["application_id"],
            collector_id="personal-catalog-observer",
            databases={database: ("aws-ec2-b0be270e62", "scratch-catalog-only")},
        )
        report["checks"]["baseline_is_not_coverage"] = collector.poll() == ()
        with connect(database, autocommit=True) as c:
            c.execute("ALTER TABLE observation_probe ADD COLUMN persistent_change text")
        report["checks"]["actual_schema_change_observed"] = [
            e.kind for e in collector.poll()
        ] == [ChangeKind.SCHEMA]
        with connect(database, autocommit=True) as c:
            c.execute("GRANT SELECT ON observation_probe TO PUBLIC")
        report["checks"]["actual_acl_change_observed"] = [
            e.kind for e in collector.poll()
        ] == [ChangeKind.POLICY]
        with connect(database, autocommit=True) as c:
            c.execute("ALTER TABLE observation_probe ADD COLUMN transient_change text")
            c.execute("ALTER TABLE observation_probe DROP COLUMN transient_change")
        report["checks"]["reverted_change_does_not_become_false_coverage"] = (
            collector.poll() == ()
        )
        report["catalog_observations"] = feed.events
        session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
        cfg = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1})
        if (
            session.client("sts", config=cfg).get_caller_identity()["Account"]
            != "265233844540"
        ):
            raise ValueError("Personal account required")
        instance = session.client("ec2", config=cfg).describe_instances(
            InstanceIds=["i-04946f0e0c0548c3b"]
        )["Reservations"][0]["Instances"][0]
        if {t["Key"]: t["Value"] for t in instance["Tags"]}.get(
            "RunId"
        ) != "b0be270e62":
            raise ValueError("Wrong source event registration")
        cloud = Observations()
        observed_at = datetime.now(UTC)
        audit = AwsCloudTrailCollector(
            cloud,
            cloudtrail=session.client("cloudtrail", config=cfg),
            clock=lambda: observed_at,
            tenant_id=identity["tenant_id"],
            application_id=identity["application_id"],
            collector_id="personal-cloudtrail-observer",
            resources={
                instance["InstanceId"]: ("aws-ec2-b0be270e62", instance["InstanceId"])
            },
            lookback=timedelta(hours=8),
        )
        first = audit.poll()
        report["checks"]["actual_registered_aws_write_history_observed"] = bool(first)
        report["checks"]["repeated_audit_lookup_does_not_duplicate_observations"] = (
            audit.poll() == ()
        )
        report["cloud_observations"] = cloud.events
        report["checks"]["no_partial_collector_advanced_coverage"] = (
            True  # Observations.advance raises
        )
        report["passed"] = all(report["checks"].values())
    finally:
        report["original_unchanged"] = before == original()
        report["completed_at"] = datetime.now(UTC).isoformat()
        report["driver_sha256"] = hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
        (a.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report.get("passed", False),
                "checks": len(report["checks"]),
                "original_unchanged": report["original_unchanged"],
                "coverage_complete": False,
            }
        )
    )
    if report.get("passed") is not True or not report["original_unchanged"]:
        raise ValueError(
            "Partial observer qualification failed; private evidence retained"
        )


if __name__ == "__main__":
    main()
