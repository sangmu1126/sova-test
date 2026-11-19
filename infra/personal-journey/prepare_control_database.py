"""Create a separate native control ledger on the owned encrypted VM PG.

Refuses existing database/role names. Application databases and writer roles
are untouched. Owner/runtime credentials remain in private local files.
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import secrets

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from sova.service import schema


DB = "sj_control_b0be270e62"
OWNER = "sj_owner_b0be270e62"
RUNTIME = "sj_runtime_b0be270e62"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--connections", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    identity = json.loads((args.inputs / "identity.json").read_text())
    links = json.loads(args.connections.read_text())
    if (identity["run_id"] != "b0be270e62" or links["run_id"] != identity["run_id"]
            or datetime.now(UTC) >= datetime.fromisoformat(identity["expires_at"])):
        raise ValueError("Unexpired personal run required")
    args.output.mkdir(mode=0o700, exist_ok=False)
    params = dict(host=identity["environments"]["target"]["database_host"], hostaddr="127.0.0.1",
        port=links["ports"]["target_db"], sslmode="verify-full", sslrootcert=str(args.inputs / "pki/ca.pem"),
        connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=1000")
    admin = make_conninfo(**params, dbname="target_orders", user="postgres",
                         password=(args.inputs / "client/target-admin.password").read_text())
    report = {"database": DB, "owner": OWNER, "runtime": RUNTIME, "business_data_changed": False,
              "migration_complete": False, "operational_qualification": False, "created": []}
    output = args.output / "result.json"

    def save():
        output.write_text(json.dumps(report, indent=2) + "\n")

    owner_secret, runtime_secret = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    for name, role, password in (("owner.dsn", OWNER, owner_secret), ("runtime.dsn", RUNTIME, runtime_secret)):
        (args.output / name).write_text(make_conninfo(**params, dbname=DB, user=role, password=password))
    save()  # Preserve exact owned partial creation, including failures; never reset to retry.
    with psycopg.connect(admin, autocommit=True) as conn:
        system_id = str(conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0])
        if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB,)).fetchone():
            raise ValueError("Control database already exists; inspect, do not reset")
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=ANY(%s)", ([OWNER, RUNTIME],)).fetchone():
            raise ValueError("Control role exists; inspect, do not reuse")
        report["pg_system_identifier"] = system_id
        for role, password in ((OWNER, owner_secret), (RUNTIME, runtime_secret)):
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
            report["created"].append(role)
            save()
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(DB), sql.Identifier(OWNER)))
        report["created"].append(DB)
        save()
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(DB)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(DB), sql.Identifier(RUNTIME)))
    with psycopg.connect((args.output / "owner.dsn").read_text()) as conn:
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.commit()
        report["migrations_applied"] = schema.apply(conn)
        schema.grant_runtime(conn, RUNTIME)
    with psycopg.connect((args.output / "runtime.dsn").read_text()) as conn:
        privileges = conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                                  "FROM pg_roles WHERE rolname=current_user").fetchone()
        if any(privileges):
            raise ValueError("Runtime has administrative privileges")
        report["runtime_admin_flags"] = list(privileges)
        report["schema_current"] = {row[0] for row in conn.execute(
            "SELECT name FROM sova_control.schema_migrations")} == set(schema.MIGRATIONS)
        report["history_delete_denied"] = not conn.execute(
            "SELECT has_table_privilege(current_user,'sova_control.effects','DELETE')").fetchone()[0]
        report["migration_insert_denied"] = not conn.execute(
            "SELECT has_table_privilege(current_user,'sova_control.schema_migrations','INSERT')").fetchone()[0]
        if not all(report[k] for k in ("schema_current", "history_delete_denied", "migration_insert_denied")):
            raise ValueError("Native ledger privilege boundary failed")
    report["verified_at"] = datetime.now(UTC).isoformat()
    save()
    print(json.dumps({k: report[k] for k in ("database", "schema_current", "history_delete_denied",
                                            "migration_insert_denied", "business_data_changed")}))


if __name__ == "__main__":
    main()
