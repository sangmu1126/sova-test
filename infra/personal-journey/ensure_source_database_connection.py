"""Re-open only a closed, registered personal SSM DB tunnel; verify actual TLS.

Never kills an existing listener, extends the trial, changes SSM idle policy or
alters a database. Existing unknown/unhealthy listeners require investigation.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import boto3
from botocore.config import Config
import psycopg
from psycopg.conninfo import make_conninfo


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections-directory", "plugin", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    inputs = json.loads((a.inputs / "identity.json").read_text())
    path = a.connections_directory / "connections.json"
    original = path.read_bytes()
    links = json.loads(original)
    if inputs["run_id"] != "b0be270e62" or links["run_id"] != inputs["run_id"]:
        raise ValueError("Wrong private source scope")
    expires = datetime.fromisoformat(inputs["expires_at"])
    if datetime.now(UTC) >= expires:
        raise ValueError("Do not extend expired trial infrastructure")
    port = links["ports"]["source_db"]
    dsn = make_conninfo(host=inputs["environments"]["source"]["database_host"], hostaddr="127.0.0.1",
        port=port, dbname="source_orders", user="postgres",
        password=(a.inputs / "client/source-admin.password").read_text(), sslmode="verify-full",
        sslrootcert=str(a.inputs / "pki/ca.pem"), connect_timeout=3,
        options="-c statement_timeout=3000")

    def verify():
        with psycopg.connect(dsn) as conn:
            row = conn.execute("SELECT (SELECT system_identifier::text FROM pg_control_system()),"
                               "current_database(),(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid())").fetchone()
            if row != ("7709720208587968539", "source_orders", True):
                raise ValueError("Connected to a different or unencrypted source")

    try:
        verify()
        recreated = False
    except psycopg.OperationalError:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            pass
        else:
            raise RuntimeError("Existing unhealthy listener; never replace an unknown process")
        if hashlib.sha256(a.plugin.read_bytes()).hexdigest() != "71258c3f7d5c15603a3b75bc8dfd62f595e8ec6ef3b8132ab59a005eebb7fa35":
            raise ValueError("Unverified session-manager-plugin")
        cfg = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})
        session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
        if session.client("sts", config=cfg).get_caller_identity()["Account"] != "265233844540":
            raise ValueError("Personal AWS only")
        instance = session.client("ec2", config=cfg).describe_instances(InstanceIds=["i-04946f0e0c0548c3b"])["Reservations"][0]["Instances"][0]
        if (instance["State"]["Name"] != "running"
                or {x["Key"]: x["Value"] for x in instance["Tags"]}.get("RunId") != inputs["run_id"]):
            raise ValueError("Registered source not running")
        request_file = a.connections_directory / "source_db-request.json"
        request = json.loads(request_file.read_text())
        if (request["Target"] != instance["InstanceId"]
                or request["DocumentName"] != "AWS-StartPortForwardingSessionToRemoteHost"
                or request["Parameters"] != {"host": ["10.246.0.10"], "portNumber": ["5432"], "localPortNumber": [str(port)]}):
            raise ValueError("Forwarding request does not match the registered database")
        with (a.artifacts / "source-db.log").open("xb") as log:
            child = subprocess.Popen(["/opt/homebrew/bin/aws", "--profile", "default", "--region", "ap-northeast-2",
                "ssm", "start-session", "--cli-input-json", "file://" + str(request_file)],
                env={**os.environ, "PATH": str(a.plugin.parent) + ":" + os.environ.get("PATH", "")},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (a.artifacts / "process.json").write_text(json.dumps({"pid": child.pid, "trial_expires_at": expires.isoformat()})+"\n")
        try:
            deadline = time.monotonic() + 25
            while True:
                if child.poll() is not None or datetime.now(UTC) >= expires:
                    raise RuntimeError("Scoped SSM connection stopped")
                try:
                    verify()
                    break
                except psycopg.OperationalError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Actual TLS database connection did not become ready") from None
                    time.sleep(0.5)
        except Exception:
            child.terminate()
            child.wait(timeout=5)
            raise
        if path.read_bytes() != original:
            raise RuntimeError("Connection metadata changed concurrently; process retained in its receipt")
        links["children"].append({"name": "source_db-explicit-reconnect", "pid": child.pid,
                                  "created_at": datetime.now(UTC).isoformat(), "log": str(a.artifacts / "source-db.log")})
        temporary = path.with_name("connections.reconnect.tmp")
        with temporary.open("x") as out:
            out.write(json.dumps(links, indent=2)+"\n")
        os.replace(temporary, path)
        recreated = True
    result = {"actual_source_tls_verified": True, "existing_listener_replaced": False,
              "closed_session_reopened": recreated, "ssm_policy_changed": False,
              "trial_extended": False, "checked_at": datetime.now(UTC).isoformat()}
    (a.artifacts / "result.json").write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
