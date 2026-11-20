"""Real PG collector guard enforcement, confined to prior isolated trial DBs.

No customer table is locked or changed. DDL/DML attempts are inside read-only
transactions and rolled back even if a guard fails. A separate scratch-table
lock tests lock_timeout; pg_sleep tests the actual statement timeout. These
are collector guard tests, not complete migration/security/continuity claims.
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from sova.adapters.kubernetes.https import private_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "trial", "artifacts"):
        parser.add_argument("--" + name, type=Path, required=True)
    a = parser.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((a.inputs / "identity.json").read_text())
    links = json.loads(a.connections.read_text())
    trial = json.loads((a.trial / "trial.json").read_text())
    if (
        identity["run_id"] != "b0be270e62"
        or links.get("ready") is not True
        or links.get("run_id") != identity["run_id"]
        or datetime.now(UTC) >= datetime.fromisoformat(identity["expires_at"])
        or trial.get("passed") is not True
        or not trial["checks"]["original_app_data_privileges_and_entry_unchanged"]
    ):
        raise ValueError(
            "Current owned transport and completed isolated trial required"
        )
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "sides": {},
        "passed": False,
        "business_migration_complete": False,
        "original_business_data_changed": False,
    }

    def connect(side, database):
        item = identity["environments"][side]
        return psycopg.connect(
            make_conninfo(
                host=item["database_host"],
                hostaddr="127.0.0.1",
                port=links["ports"][side + "_db"],
                dbname=database,
                user="postgres",
                password=private_text(
                    a.inputs / ("client/" + side + "-admin.password")
                ),
                sslmode="verify-full",
                sslrootcert=str(a.inputs / "pki/ca.pem"),
                connect_timeout=5,
            )
        )

    def bounded(c):
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        c.execute("SET LOCAL statement_timeout='5s'")
        c.execute("SET LOCAL lock_timeout='1s'")

    def counts(side):
        with connect(side, side + "_orders") as c:
            return list(
                c.execute(
                    "SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),"
                    "(SELECT count(*) FROM idempotency_results)"
                ).fetchone()
            )

    before = {side: counts(side) for side in ("source", "target")}
    try:
        for side, prefix, expected in (
            ("source", "q_src_other_", "7709720208587968539"),
            ("target", "q_tgt_other_", "7709723763091251229"),
        ):
            files = list(a.trial.glob(prefix + "*.dsn"))
            if len(files) != 1 or not re.fullmatch(
                prefix + r"[a-f0-9]{10}", files[0].stem
            ):
                raise ValueError("Exact isolated conformance database required")
            previous = conninfo_to_dict(private_text(files[0]))
            if (
                previous.get("dbname") != files[0].stem
                or previous.get("user") != "postgres"
            ):
                raise ValueError("Scratch database binding differs")
            database = files[0].stem
            result = {"database": database, "checks": {}}
            report["sides"][side] = result
            with connect(side, database) as c:
                system, tls = c.execute(
                    "SELECT (SELECT system_identifier::text FROM pg_control_system()),"
                    "(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid())"
                ).fetchone()
                if (system, tls) != (expected, True):
                    raise ValueError("Actual TLS database identity differs")
                c.rollback()
                for name, query in (
                    (
                        "persistent_ddl_refused",
                        sql.SQL("CREATE TABLE public.{} (id integer)").format(
                            sql.Identifier("sova_guard_" + uuid4().hex)
                        ),
                    ),
                    ("dml_refused", sql.SQL("DELETE FROM public.orders WHERE false")),
                    ("statement_timeout", sql.SQL("SELECT pg_sleep(6)")),
                ):
                    bounded(c)
                    started = time.monotonic()
                    try:
                        c.execute(query)
                    except psycopg.Error as exc:
                        code = exc.sqlstate
                        expected_code = (
                            "57014" if name == "statement_timeout" else "25006"
                        )
                        if code != expected_code:
                            raise ValueError(
                                "Unexpected SQL guard refusal: " + str(code)
                            ) from None
                        result["checks"][name] = {
                            "sqlstate": code,
                            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                        }
                    else:
                        raise ValueError(
                            "Required real SQL guard did not refuse " + name
                        )
                    finally:
                        c.rollback()
                with connect(side, database) as holder:
                    holder.execute("LOCK TABLE public.orders IN ACCESS EXCLUSIVE MODE")
                    bounded(c)
                    started = time.monotonic()
                    try:
                        c.execute("SELECT count(*) FROM public.orders")
                    except psycopg.errors.LockNotAvailable as exc:
                        if exc.sqlstate != "55P03":
                            raise
                        result["checks"]["lock_timeout"] = {
                            "sqlstate": exc.sqlstate,
                            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                        }
                    else:
                        raise ValueError(
                            "Collector waited through the exclusive scratch lock"
                        )
                    finally:
                        c.rollback()
                        holder.rollback()
            result["passed"] = len(result["checks"]) == 4
        report["passed"] = all(item["passed"] for item in report["sides"].values())
    finally:
        try:
            after = {side: counts(side) for side in ("source", "target")}
        except (psycopg.Error, OSError) as exc:
            after = None
            report["passed"] = False
            report["after_readback_error"] = type(exc).__name__
        report["original_counts_before"], report["original_counts_after"] = (
            before,
            after,
        )
        report["original_business_data_changed"] = before != after
        report["completed_at"] = datetime.now(UTC).isoformat()
        (a.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"] or before != after:
        raise ValueError(
            "Real collector guard trial did not pass or original data changed"
        )
    print(
        json.dumps(
            {
                "actual_guard_refusals": 8,
                "original_business_data_changed": False,
                "migration_complete": False,
            }
        )
    )


if __name__ == "__main__":
    main()
