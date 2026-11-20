"""Rebind existing owned ledger credentials to fresh verified TLS transport.

Reads exact system/database/role identities, schema and zero admission counts.
No schema, role/password, trust, registry, application data or route mutation.
Old private DSNs and expired certificates remain untouched.
"""

import argparse
import hashlib
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from sova.adapters.kubernetes.https import private_text
from sova.service import schema


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "previous", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    identity = json.loads((a.inputs / "identity.json").read_text())
    links = json.loads(a.connections.read_text())
    if (
        identity["run_id"] != "b0be270e62"
        or links["run_id"] != identity["run_id"]
        or links.get("ready") is not True
        or datetime.now(UTC) >= datetime.fromisoformat(identity["expires_at"])
    ):
        raise ValueError("Owned fresh actual transport required")
    target = identity["environments"]["target"]
    ca = str(a.inputs / "pki/ca.pem")
    admin = make_conninfo(
        host=target["database_host"],
        hostaddr="127.0.0.1",
        port=links["ports"]["target_db"],
        dbname="target_orders",
        user="postgres",
        password=private_text(a.inputs / "client/target-admin.password"),
        sslmode="verify-full",
        sslrootcert=ca,
        connect_timeout=5,
    )
    with psycopg.connect(admin) as connection:
        if (
            connection.execute(
                "SELECT system_identifier::text FROM pg_control_system()"
            ).fetchone()[0]
            != "7709723763091251229"
        ):
            raise ValueError("Wrong target server")
    a.output.mkdir(mode=0o700, exist_ok=False)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "database": "sj_control_b0be270e62",
        "roles": {},
        "mutations": False,
        "migration_complete": False,
        "passwords_preserved": True,
    }
    for kind in ("owner", "runtime"):
        old = conninfo_to_dict(private_text(a.previous / (kind + ".dsn")))
        expected = "sj_" + kind + "_b0be270e62"
        if (
            old.get("dbname") != report["database"]
            or old.get("user") != expected
            or old.get("host") != target["database_host"]
            or old.get("hostaddr") != "127.0.0.1"
            or old.get("sslmode") != "verify-full"
        ):
            raise ValueError("Original ledger binding differs")
        new = make_conninfo(
            **{**old, "port": links["ports"]["target_db"], "sslrootcert": ca}
        )
        with psycopg.connect(new) as connection:
            db, user = connection.execute(
                "SELECT current_database(),current_user"
            ).fetchone()
            if (db, user) != (report["database"], expected):
                raise ValueError("Actual ledger identity differs")
            connection.execute(
                "SELECT set_config('sova.tenant_id',%s,true)", (identity["tenant_id"],)
            )
            counts = {
                name: connection.execute(
                    "SELECT count(*) FROM sova_control." + name
                ).fetchone()[0]
                for name in ("plans", "operations", "effects")
            }
            if any(counts.values()):
                raise ValueError(
                    "Existing admitted work requires coordinated connection handoff"
                )
            if not connection.execute(
                "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
            ).fetchone()[0]:
                raise ValueError("Ledger TLS required")
            versions = connection.execute(
                "SELECT name,sha256 FROM sova_control.schema_migrations ORDER BY name"
            ).fetchall()
            expected_migrations = {
                name: hashlib.sha256(schema.text_of(name).encode()).hexdigest()
                for name in schema.MIGRATIONS
            }
            if (
                len(versions) != len(expected_migrations)
                or dict(versions) != expected_migrations
            ):
                raise ValueError(
                    "Exact ledger migration names and source hashes required"
                )
            flags = connection.execute(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            if flags != (False, False):
                raise ValueError("Owner/runtime cannot have superuser or RLS bypass")
        (a.output / (kind + ".dsn")).write_text(new)
        report["roles"][kind] = {
            "identity_verified": True,
            "ssl": True,
            "admission_counts": counts,
            "migration_count": len(versions),
            "superuser": False,
            "bypass_rls": False,
        }
    report["finished_at"] = datetime.now(UTC).isoformat()
    (a.output / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
