"""Prepare only the dedicated target's LOGIN prerequisite, keeping writes shut.

Database-local grants are revoked before enabling the existing target app
role. The batch role stays NOLOGIN. Source business data, writer roles and
entry routing are never changed. Fresh actual writer sessions verify refusal.
Also prepares explicitly owned source scratch/other databases for failback
probes; these empty databases are not geographical or backup qualifications.
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "control-directory", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise ValueError("Preserve previous preparation evidence")
    inputs = json.loads((args.inputs / "identity.json").read_text())
    links = json.loads(args.connections.read_text())
    if inputs["run_id"] != "b0be270e62" or datetime.now(UTC) >= datetime.fromisoformat(inputs["expires_at"]):
        raise ValueError("Unexpired personal run required")

    def connect(side, *, role="postgres", database=None, autocommit=False):
        entry = inputs["environments"][side]
        suffix = "admin" if role == "postgres" else ("app" if role == "orders_app" else "batch")
        return psycopg.connect(make_conninfo(host=entry["database_host"], hostaddr="127.0.0.1",
            port=links["ports"][side + "_db"], dbname=database or entry["database"], user=role,
            password=(args.inputs / ("client/" + side + "-" + suffix + ".password")).read_text(),
            sslmode="verify-full", sslrootcert=str(args.inputs / "pki/ca.pem"), connect_timeout=5,
            options="-c statement_timeout=10000 -c lock_timeout=2000"), autocommit=autocommit)

    with psycopg.connect((args.control_directory / "runtime.dsn").read_text()) as conn:
        conn.execute("SELECT set_config('sova.tenant_id',%s,true)", (inputs["tenant_id"],))
        counts = [conn.execute("SELECT count(*) FROM sova_control." + table).fetchone()[0]
                  for table in ("plans", "operations", "effects")]
        if counts != [0, 0, 0]:
            raise ValueError("Refuse prerequisite changes after an application plan exists")
    report = {"run_id": inputs["run_id"], "started_at": datetime.now(UTC).isoformat(),
        "plan_operation_effect_counts": counts, "business_data_changed": False,
        "traffic_changed": False, "target_writes_enabled": False, "migration_complete": False,
        "completed": False, "stage": "before_changes"}

    def checkpoint(stage):
        report["stage"] = stage
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    for side, expected_id in (("source", "7709720208587968539"), ("target", "7709723763091251229")):
        with connect(side) as conn:
            found = conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
            if found != expected_id:
                raise ValueError("Registered PostgreSQL system was replaced")
            roles = conn.execute("SELECT rolname,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,"
                "EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) FROM pg_roles "
                "WHERE rolname IN ('orders_app','orders_batch') ORDER BY rolname").fetchall()
            if len(roles) != 2 or any(any(row[2:]) for row in roles):
                raise ValueError("Unexpected writer privilege or membership")
            data = list(conn.execute("SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),"
                "(SELECT count(*) FROM idempotency_results)").fetchone())
            if data != ([1, 1, 1] if side == "source" else [0, 0, 0]):
                raise ValueError("Unexpected business state before writer preparation")
            report[side] = {"system_identifier": found, "counts_before": data,
                            "writer_login_before": {row[0]: row[1] for row in roles}}
    checkpoint("verified_identities_before_target_preparation")
    with connect("target") as conn:
        conn.execute("REVOKE CONNECT ON DATABASE target_orders FROM PUBLIC,orders_app,orders_batch")
        conn.execute("REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON ALL TABLES IN SCHEMA public FROM PUBLIC,orders_app,orders_batch")
        conn.execute("REVOKE USAGE,UPDATE ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC,orders_app,orders_batch")
        conn.execute("ALTER ROLE orders_app LOGIN")
        if conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname='orders_batch'").fetchone() != (False,):
            raise ValueError("Batch role must remain NOLOGIN")
    report["target_app_login_prerequisite"] = True
    checkpoint("target_login_prepared_with_writes_closed")
    refusals = {}
    for role in ("orders_app", "orders_batch"):
        try:
            with connect("target", role=role):
                raise ValueError("Target writer unexpectedly connected")
        except psycopg.OperationalError as exc:
            reason = str(exc).lower()
            expected = ("permission denied for database" if role == "orders_app" else "is not permitted to log in")
            if expected not in reason:
                raise ValueError("Refusal is not attributable to the expected writer policy") from None
            refusals[role] = {"denied": True, "reason": "database_connect_revoked" if role == "orders_app" else "role_nologin"}
    report["actual_target_writer_refusals"] = refusals
    checkpoint("fresh_target_writer_refusals_verified")
    marker = "sova.run=b0be270e62;purpose=failback-probe;not-residency-evidence"
    with connect("source", autocommit=True) as conn:
        for database in ("scratch_source", "other_source"):
            existing = conn.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s", (database,)).fetchone()
            if existing is not None and existing[0] != marker:
                raise ValueError("Source probe database exists without this run's ownership marker")
            if existing is None:
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
                conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(database), sql.Literal(marker)))
            conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC,orders_app,orders_batch").format(sql.Identifier(database)))
    report["source_probe_databases"] = ["scratch_source", "other_source"]
    with connect("source", role="orders_app") as conn:
        if conn.execute("SELECT count(*) FROM orders").fetchone() != (1,):
            raise ValueError("Source application state changed")
    report["source_actual_writer_connection_preserved"] = True
    report["completed"] = True
    report["completed_at"] = datetime.now(UTC).isoformat()
    checkpoint("prepared_not_admitted")
    print(json.dumps({"target_app_login": True, "actual_target_writer_refusals": refusals,
                      "target_writes_enabled": False, "source_business_changed": False}))


if __name__ == "__main__":
    main()
