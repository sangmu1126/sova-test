"""Create one NEW append-only observation role in the owned control DB.

Only this role receives the new restrictive policies. Existing host privileges,
schemas, migration history, application databases and business routes stay as-is.
The role cannot certify a watermark, operate plans or delete observations.
Passwords stay in a new private DSN file; role/password and row access expire at
the existing trial deadline. No role is reset, reused or automatically removed.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from sova.adapters.kubernetes.https import private_text
from sova.adapters.postgres_change_feed import PostgresChangeFeed


RUN = "b0be270e62"
DB = "sj_control_" + RUN
ROLE = "sj_observe_" + RUN
SOURCE = "aws-ec2-" + RUN
TARGET = "local-rke2-" + RUN
COLLECTORS = ("personal-durable-catalog-v1", "personal-durable-cloudtrail-v2")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((a.inputs / "identity.json").read_text())
    links = json.loads(a.connections.read_text())
    until = datetime.fromisoformat(identity["expires_at"])
    if identity["run_id"] != RUN or links["run_id"] != RUN or links["ready"] is not True or datetime.now(UTC) >= until:
        raise ValueError("Unexpired connected owned trial required")
    target = identity["environments"]["target"]
    args = dict(host=target["database_host"], hostaddr="127.0.0.1", port=links["ports"]["target_db"],
                dbname=DB, sslmode="verify-full", sslrootcert=str(a.inputs / "pki/ca.pem"),
                connect_timeout=5, options="-c statement_timeout=5000 -c lock_timeout=1000")
    password = secrets.token_urlsafe(40)
    observer = make_conninfo(**args, user=ROLE, password=password)
    report = {"version": 1, "run_id": RUN, "role": ROLE, "database": DB,
              "expires_at": until.isoformat(), "created": False, "checks": {},
              "existing_roles_changed": False, "business_data_changed": False,
              "complete_feed_authority_granted": False, "migration_complete": False}
    output = a.artifacts / "result.json"

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")

    save()
    admin = make_conninfo(**args, user="postgres", password=private_text(a.inputs / "client/target-admin.password"))
    with psycopg.connect(admin) as c:
        if c.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0] != "7709723763091251229":
            raise ValueError("Wrong physical control database server")
        if c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (ROLE,)).fetchone():
            raise ValueError("Preserve existing observer role; inspect instead of resetting")
        existing = c.execute("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class JOIN pg_namespace n ON n.oid=relnamespace WHERE n.nspname='sova_control' AND relname IN ('change_events','change_feed_watermarks')").fetchall()
        if len(existing) != 2 or not all(r[1] and r[2] for r in existing):
            raise ValueError("Existing forced row security required")
        # Save before creating anything; interrupted provisioning preserves the
        # exact secret privately and never silently rotates a partially made role.
        fd = os.open(a.artifacts / "observer.dsn", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(observer)
        c.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 4 VALID UNTIL {} PASSWORD {}").format(sql.Identifier(ROLE), sql.Literal(until.isoformat()), sql.Literal(password)))
        c.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(DB), sql.Identifier(ROLE)))
        c.execute(sql.SQL("GRANT USAGE ON SCHEMA sova_control TO {}").format(sql.Identifier(ROLE)))
        c.execute(sql.SQL("GRANT SELECT,INSERT ON sova_control.change_events TO {}").format(sql.Identifier(ROLE)))
        c.execute(sql.SQL("GRANT SELECT ON sova_control.change_feed_watermarks TO {}").format(sql.Identifier(ROLE)))
        event_scope = sql.SQL("tenant_id={} AND application_id={} AND environment_id={} AND collector_id IN ({},{}) AND clock_timestamp()<{}::timestamptz").format(
            sql.Literal(identity["tenant_id"]), sql.Literal(identity["application_id"]), sql.Literal(SOURCE),
            *(sql.Literal(x) for x in COLLECTORS), sql.Literal(until))
        event_scope = sql.SQL("({}) AND artifact_digest IS NULL AND cause_plan_digest IS NULL AND cause_step_id IS NULL AND observed_at<=clock_timestamp() AND ((collector_id={} AND kind IN ('schema','policy')) OR (collector_id={} AND kind IN ('resource','policy','unclassified')))").format(
            event_scope, *(sql.Literal(x) for x in COLLECTORS))
        watermark_scope = sql.SQL("tenant_id={} AND environment_id IN ({},{}) AND clock_timestamp()<{}::timestamptz").format(
            sql.Literal(identity["tenant_id"]), sql.Literal(SOURCE), sql.Literal(TARGET), sql.Literal(until))
        for table, predicate in (("change_events", event_scope), ("change_feed_watermarks", watermark_scope)):
            c.execute(sql.SQL("CREATE POLICY {} ON sova_control.{} AS RESTRICTIVE FOR ALL TO {} USING ({}) WITH CHECK ({})").format(
                sql.Identifier("observe_" + RUN + "_" + table), sql.Identifier(table), sql.Identifier(ROLE), predicate, predicate))
    report["created"] = True
    save()
    try:
        with psycopg.connect(observer) as c:
            flags = c.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,(SELECT count(*) FROM pg_auth_members WHERE member=pg_roles.oid),rolvaliduntil FROM pg_roles WHERE rolname=current_user").fetchone()
            report["checks"]["non_admin_no_memberships_and_expiring_password"] = flags[:6] == (False, False, False, False, False, 0) and flags[6] == until
            c.execute("SELECT set_config('sova.tenant_id',%s,true)", (identity["tenant_id"],))
            checks = {
                "delete_history_denied": ("DELETE FROM sova_control.change_events WHERE FALSE", ()),
                "update_history_denied": ("UPDATE sova_control.change_events SET subject=subject WHERE FALSE", ()),
                "watermark_insert_denied": ("INSERT INTO sova_control.change_feed_watermarks(tenant_id,environment_id,collector_id,observed_through) VALUES (%s,%s,%s,clock_timestamp())", (identity["tenant_id"], SOURCE, COLLECTORS[1])),
                "watermark_update_denied": ("UPDATE sova_control.change_feed_watermarks SET observed_through=clock_timestamp() WHERE FALSE", ()),
                "plans_write_denied": ("DELETE FROM sova_control.plans WHERE FALSE", ()),
                "effects_read_denied": ("SELECT 1 FROM sova_control.effects LIMIT 1", ()),
                "schema_create_denied": ("CREATE TABLE sova_control.observer_must_not_create(id int)", ()),
            }
            for name, (statement, parameters) in checks.items():
                refused = False
                try:
                    with c.transaction(force_rollback=True):
                        c.execute(statement, parameters)
                except psycopg.errors.InsufficientPrivilege:
                    refused = True
                report["checks"][name] = refused
            # Every attempted insertion is rolled back even if a broken policy
            # unexpectedly permits it. No synthetic event is committed as evidence.
            for name, changed in (("other_tenant_insert_denied", {"tenant": "other-" + uuid4().hex}),
                                  ("other_application_insert_denied", {"app": "other-application"}),
                                  ("other_environment_insert_denied", {"environment": TARGET}),
                                  ("unregistered_collector_insert_denied", {"collector": "unregistered"})):
                values = {"tenant": identity["tenant_id"], "app": identity["application_id"], "environment": SOURCE, "collector": COLLECTORS[1], **changed}
                refused = False
                try:
                    with c.transaction(force_rollback=True):
                        c.execute("SELECT set_config('sova.tenant_id',%s,true)", (values["tenant"],))
                        c.execute("INSERT INTO sova_control.change_events(tenant_id,event_id,application_id,kind,environment_id,observed_at,collector_id) VALUES (%s,%s,%s,'unclassified',%s,clock_timestamp(),%s)",
                                  (values["tenant"], "denied-probe-" + uuid4().hex, values["app"], values["environment"], values["collector"]))
                except psycopg.errors.InsufficientPrivilege:
                    refused = True
                report["checks"][name] = refused
            report["checks"]["business_database_connect_not_granted"] = c.execute("SELECT has_database_privilege(current_user,'target_orders','CONNECT')").fetchone()[0] is False
            for name, kind, digest, cause, at in (
                ("registry_claim_insert_denied", "contract", "a" * 64, None, "clock_timestamp()"),
                ("plan_cause_attribution_denied", "unclassified", None, "b" * 64, "clock_timestamp()"),
                ("future_observation_denied", "unclassified", None, None, "clock_timestamp()+interval '1 hour'"),
            ):
                refused = False
                try:
                    with c.transaction(force_rollback=True):
                        c.execute("INSERT INTO sova_control.change_events(tenant_id,event_id,application_id,kind,environment_id,observed_at,collector_id,artifact_digest,cause_plan_digest,cause_step_id) VALUES (%s,%s,%s,%s,%s," + at + ",%s,%s,%s,%s)",
                                  (identity["tenant_id"], "denied-probe-" + uuid4().hex, identity["application_id"], kind, SOURCE, COLLECTORS[1], digest, cause, "step" if cause else None))
                except psycopg.errors.InsufficientPrivilege:
                    refused = True
                report["checks"][name] = refused
        feed = PostgresChangeFeed(lambda: psycopg.connect(observer), tenant_id=identity["tenant_id"])
        events = feed.events(application_id=identity["application_id"], environments=(SOURCE,), since=datetime(2026, 11, 20, tzinfo=UTC))
        report["checks"]["scoped_real_observations_readable"] = bool(events)
        if events:
            with feed.transaction() as c:
                collector = c.execute("SELECT collector_id FROM sova_control.change_events WHERE event_id=%s", (events[0].event_id,)).fetchone()[0]
            report["checks"]["same_real_event_idempotent_append_allowed"] = feed.record(events[0], collector_id=collector) is False
    except Exception:
        with psycopg.connect(admin) as c:
            c.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(ROLE)))
        report["new_role_disabled_after_validation_exception"] = True
        save()
        raise
    report["completed_at"] = datetime.now(UTC).isoformat()
    report["driver_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["passed"] = all(report["checks"].values())
    save()
    print(json.dumps({"role": ROLE, "checks": len(report["checks"]), "passed": report["passed"], "complete_feed_authority_granted": False}))
    if not report["passed"]:
        with psycopg.connect(admin) as c:
            c.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(ROLE)))
        report["new_role_disabled_after_failed_validation"] = True
        save()
        raise ValueError("New observer role boundary failed; preserve for investigation")


if __name__ == "__main__":
    main()
