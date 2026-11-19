"""Actual AWS/local-VM canary preflight, NOT a migration completion receipt.

One explicit source business key is written and replayed. Destination writers
remain fenced. No credential/role/route mutation and no synthetic PASS input.
"""

import argparse
from datetime import UTC, datetime
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess

import psycopg
from psycopg.conninfo import make_conninfo


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--connections", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((args.inputs / "identity.json").read_text())
    links = json.loads(args.connections.read_text())
    if (identity["run_id"] != "b0be270e62" or links["run_id"] != identity["run_id"]
            or not links["ready"] or datetime.now(UTC) >= datetime.fromisoformat(identity["expires_at"])):
        raise ValueError("Wrong, expired or disconnected personal run")
    ca = args.inputs / "pki/ca.pem"
    token = (args.inputs / "client/client.token").read_text()
    headers = {"authorization": "Bearer " + token, "x-qualification-tenant": identity["tenant_id"],
        "x-qualification-run": identity["run_id"], "idempotency-key": "aws-before-1",
        "content-type": "application/json", "connection": "close"}
    report = {"run_id": identity["run_id"], "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "checks": {}, "databases": {}, "http": {}, "migration_complete": False,
              "operational_qualification": False, "writes": []}
    passed = False

    def request(side, method, path, body=None, override=None):
        host = identity["environments"][side]["application_host"]
        context = ssl.create_default_context(cafile=str(ca))
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        raw = socket.create_connection(("127.0.0.1", links["ports"][side + "_app"]), timeout=5)
        verified = context.wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPSConnection(host, timeout=5, context=context)
        conn.sock = verified
        protocol = verified.version()
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body),
                         headers={**headers, **(override or {})})
            response = conn.getresponse()
            data = response.read(8193)
            if len(data) > 8192:
                raise ValueError("Unbounded canary response")
            result = json.loads(data)
            if response.getheader("x-sova-deployment") != identity["app_manifest"] or response.getheader("x-sova-side") != side:
                raise ValueError("The actual HTTPS endpoint is not the registered artifact/side")
            report["http"][side] = {"verified_hostname": host, "tls_protocol": protocol,
                                   "claimed_deployment_digest": response.getheader("x-sova-deployment")}
            return response.status, result
        finally:
            conn.close()

    def dsn(side, user="postgres", password=None):
        env = identity["environments"][side]
        return make_conninfo(host=env["database_host"], hostaddr="127.0.0.1",
            port=links["ports"][side + "_db"], user=user, password=password or
            (args.inputs / ("client/" + side + "-admin.password")).read_text(),
            dbname=env["database"], sslmode="verify-full", sslrootcert=str(ca), connect_timeout=3,
            options="-c statement_timeout=5000 -c lock_timeout=1000")

    try:
        for side in ("source", "target"):
            with psycopg.connect(dsn(side)) as conn:
                row = conn.execute("SELECT current_database(),current_setting('server_version'),"
                    "(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()),"
                    "(SELECT system_identifier::text FROM pg_control_system())").fetchone()
                roles = conn.execute("SELECT rolname,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,"
                    "rolreplication,rolbypassrls,EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) "
                    "FROM pg_roles WHERE rolname IN ('orders_app','orders_batch') ORDER BY rolname").fetchall()
                if row[0] != identity["environments"][side]["database"] or not row[2] or len(roles) != 2:
                    raise ValueError("Wrong actual database identity/TLS/writer inventory")
                if any(any(role[2:]) or role[1] != (side == "source") for role in roles):
                    raise ValueError("Unexpected privilege or pre-cutover writer state")
                report["databases"][side] = {"database": row[0], "version": row[1], "verified_tls": row[2],
                    "system_identifier": row[3], "writers": [{"role": r[0], "login": r[1]} for r in roles]}
            report["checks"][side + "_verified_database_and_writer_scope"] = True
            code, health = request(side, "GET", "/healthz")
            report["checks"][side + "_verified_https_identity"] = (code == 200 and health == {
                "application_id": identity["application_id"], "side": side})
        report["checks"]["independent_database_systems"] = (
            report["databases"]["source"]["system_identifier"] != report["databases"]["target"]["system_identifier"])
        body = {"customer": "personal-canary", "amount": "17.25"}
        denied, _ = request("source", "POST", "/orders", body, {"authorization": ""})
        report["checks"]["source_unauthorized_denied"] = denied == 401
        if denied != 401:
            raise RuntimeError("Unexpected unauthenticated outcome; stop before more business requests")
        target_denied, _ = request("target", "POST", "/orders", body)
        report["checks"]["target_writer_still_fenced"] = target_denied == 503
        if target_denied != 503:
            raise RuntimeError("Unexpected target outcome; stop before source writes")
        report["writes"].append({"side": "source", "business_key": "personal-canary/aws-before-1", "outcome": "UNKNOWN"})
        first = request("source", "POST", "/orders", body)
        second = request("source", "POST", "/orders", body)
        lookup = request("source", "GET", "/orders?customer=personal-canary&idempotency_key=aws-before-1")
        report["checks"]["source_actual_request_replay_and_lookup"] = (
            first[0] == second[0] == lookup[0] == 200 and first[1] == second[1] == lookup[1])
        report["writes"][-1].update({"outcome": "OBSERVED", "receipt": first[1], "http_attempts": 2})
        for side in ("source", "target"):
            with psycopg.connect(dsn(side)) as conn:
                counts = conn.execute("SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),"
                    "(SELECT count(*) FROM idempotency_results)").fetchone()
                report["databases"][side]["business_counts"] = list(counts)
                report["checks"][side + "_business_effect_count"] = counts == ((1, 1, 1) if side == "source" else (0, 0, 0))
            try:
                with psycopg.connect(dsn(side), host="unregistered.invalid"):
                    report["checks"][side + "_wrong_hostname_denied"] = False
            except psycopg.OperationalError as error:
                message = str(error).lower()
                report["checks"][side + "_wrong_hostname_denied"] = (
                    "does not match host name" in message or "hostname mismatch" in message)
            try:
                # Never provide a real password on this deliberate plaintext path.
                with psycopg.connect(dsn(side), sslmode="disable", password=""):
                    report["checks"][side + "_plaintext_without_password_denied"] = False
            except psycopg.OperationalError as error:
                message = str(error).lower()
                report["checks"][side + "_plaintext_without_password_denied"] = (
                    "pg_hba.conf" in message and ("no encryption" in message or "ssl off" in message))
        passed = bool(report["checks"]) and all(report["checks"].values())
    finally:
        report["passed"] = passed
        report["at"] = datetime.now(UTC).isoformat()
        (args.artifacts / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": passed, "checks": len(report["checks"]),
        "real_source_business_effects": report["databases"]["source"]["business_counts"],
        "target_writers_fenced": report["checks"]["target_writer_still_fenced"], "migration_complete": False}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
