"""Actual native host process + encrypted VM ledger, without admission.

Confirms loopback health, actual owner identity, runtime SQL refusals and no
external effects. This does not qualify a migration or a remote crash recovery.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import psycopg


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--environment", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    env = {**os.environ, **json.loads(args.environment.read_text())}
    if env["SOVA_WORKER_IDENTITY"] != "host" or any(env.get(k) for k in ("POD_NAME", "POD_UID", "POD_NAMESPACE")):
        raise ValueError("Explicit real host profile without Pod identity required")
    dsn = Path(env["SOVA_DATABASE_URL_FILE"]).read_text()
    tenant = env["SOVA_TENANTS"]
    report = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "started_at": datetime.now(UTC).isoformat(), "checks": {}, "admitted": False,
        "migration_complete": False, "operational_qualification": False}

    def counts():
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            if conn.execute("SELECT current_database()").fetchone()[0] != "sj_control_b0be270e62":
                raise ValueError("Not the owned control ledger")
            conn.execute("SELECT set_config('sova.tenant_id',%s,true)", (tenant,))
            return [conn.execute("SELECT count(*) FROM sova_control." + name).fetchone()[0]
                    for name in ("operations", "effects")]

    before = counts()
    if before != [0, 0]:
        raise ValueError("Refuse to run startup qualification with admitted work")
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        for name, statement in (
            ("history_delete_denied", "DELETE FROM sova_control.effects WHERE FALSE"),
            ("migration_insert_denied", "INSERT INTO sova_control.schema_migrations(name,sha256) "
             "SELECT 'qualification-denied','0000000000000000000000000000000000000000000000000000000000000000' WHERE FALSE"),
            ("create_schema_denied", "CREATE SCHEMA sj_forbidden_qualification")):
            try:
                with conn.transaction(force_rollback=True):
                    conn.execute(statement)
            except psycopg.errors.InsufficientPrivilege as exc:
                report["checks"][name] = exc.sqlstate == "42501"
            else:
                raise ValueError("Unexpected runtime privilege: " + name)
    try:
        with psycopg.connect(dsn, dbname="target_orders", connect_timeout=5) as conn:
            try:
                conn.execute("SELECT id FROM orders LIMIT 1")
            except psycopg.errors.InsufficientPrivilege as exc:
                report["checks"]["runtime_business_data_read_denied"] = exc.sqlstate == "42501"
                report["business_denial_mechanism"] = "table_privilege"
            else:
                raise ValueError("Control runtime unexpectedly reads business data")
    except psycopg.OperationalError as exc:
        # A reachable, authenticated ledger on this same server was the positive
        # control above. Never accept TLS/timeouts as a business-data denial.
        text = str(exc)
        if ('permission denied for database "target_orders"' not in text
                or 'does not have CONNECT privilege' not in text):
            raise
        report["checks"]["runtime_business_data_read_denied"] = True
        report["business_denial_mechanism"] = "database_connect_privilege"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    worker = None
    try:
        with (args.artifacts / "worker.log").open("xb") as log:
            worker = subprocess.Popen([sys.executable, "-m", "sova.service.main", "run-worker", "--health-port", str(port),
                                       "--poll-seconds", "0.5"], env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 20
        successes = 0
        while successes < 3:
            if worker.poll() is not None:
                raise RuntimeError("Native host worker stopped; inspect private log")
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                try:
                    c.request("GET", "/healthz")
                    response = c.getresponse()
                    good = response.status == 200 and json.loads(response.read(4096))["status"] == "ok"
                finally:
                    c.close()
                if good:
                    successes += 1
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Actual native worker health missing")
            time.sleep(0.5)
        text = (args.artifacts / "worker.log").read_text()
        match = re.search(r"worker (host\." + str(worker.pid) + r"\.[0-9a-f]{12}) serving", text)
        if not match:
            raise ValueError("Actual process identity not found")
        report["owner"] = match[1]
        report["checks"]["actual_native_host_health"] = True
        worker.terminate()
        worker.wait(timeout=30)
        report["checks"]["graceful_exit"] = worker.returncode == 0
        report["checks"]["no_admission_or_external_effects"] = counts() == before == [0, 0]
        if not all(report["checks"].values()):
            raise ValueError("Native worker startup boundary failed")
    finally:
        if worker is not None and worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=30)
        report["finished_at"] = datetime.now(UTC).isoformat()
        (args.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
