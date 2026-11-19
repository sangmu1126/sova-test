"""Real native OCI HTTP/SCRAM/TLS checks on disposable, isolated containers.

No AWS, VM, shared DB, synthetic TLS result or production qualification.
Credentials reach containers only through private files/stdin, never argv.
"""

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import time
from uuid import uuid4

from qualification_postgres import certificates
from postgres_business import SCHEMA

ERROR_DIRECTORY = None


def docker(*args, data=None):
    result = subprocess.run(["docker", *args], input=data, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if result.returncode:
        # Keep driver errors private; they can include credential-bearing SQL.
        if ERROR_DIRECTORY is not None:
            (ERROR_DIRECTORY / ("docker-error-" + uuid4().hex + ".txt")).write_text(result.stderr[:1048576])
        raise RuntimeError("Docker qualification operation failed")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-directory", type=Path, required=True)
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.postgres_image):
        parser.error("Exact cached PostgreSQL image ID required")
    args.artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)
    global ERROR_DIRECTORY
    ERROR_DIRECTORY = args.artifacts
    image = json.loads((args.image_directory / "image.json").read_text())
    scan = json.loads((args.image_directory / "scan-summary.json").read_text())
    if (scan["manifest_digest"] != image["manifest_digest"] or scan["preflight"] != "PASS"
            or any(scan["counts"].values())):
        raise ValueError("This run requires an all-severity-zero preparatory scan")
    archive = args.image_directory / "canary.oci.tar"
    with archive.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != image["archive_sha256"]:
            raise ValueError("OCI artifact changed after build")
    docker("load", "--input", str(archive))
    ref = image["manifest_digest"]
    inspect = json.loads(docker("image", "inspect", ref))[0]
    if inspect["Architecture"] != "arm64" or inspect["Config"]["User"] != "10001:10001":
        raise ValueError("Native non-root canary image required")
    pg_image = json.loads(docker("image", "inspect", args.postgres_image))[0]
    if pg_image["Architecture"] != "arm64":
        raise ValueError("Native PostgreSQL image required")

    run = "canary-image-" + uuid4().hex
    network, pg, app, init, volume = ["sova-" + run + "-" + kind
                                      for kind in ("network", "db", "app", "init", "config")]
    pg_init, pg_volume = "sova-" + run + "-pg-init", "sova-" + run + "-pg-config"
    report = {"run_id": run, "manifest_digest": ref,
              "config_digest": image["config_digest"], "postgres_image": args.postgres_image,
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "tests": {}, "operational_qualification": False, "aws_vm_migration": False}
    created_network = False
    created_volumes = []
    attempted = []
    passed = False
    try:
        with tempfile.TemporaryDirectory(prefix="sova-canary-image-") as directory:
            root = Path(directory)
            certificates(root)
            (root / "untrusted").mkdir(mode=0o700)
            certificates(root / "untrusted")
            (root / "server/pg_hba.conf").write_text(
                "local all all trust\nhostnossl all all 0.0.0.0/0 reject\n"
                "hostssl all all 0.0.0.0/0 scram-sha-256\n")
            (root / "server/admin-password").write_text(secrets.token_urlsafe(40))
            docker("network", "create", "--internal", "--label", "sova.qualification=" + run, network)
            created_network = True
            docker("volume", "create", "--label", "sova.qualification=" + run, pg_volume)
            created_volumes.append(pg_volume)
            attempted.append(pg_init)
            docker("run", "--name", pg_init, "--label", "sova.qualification=" + run,
                "--network", "none", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                "--cap-add", "DAC_OVERRIDE",
                "--security-opt", "no-new-privileges", "--mount", f"type=volume,src={pg_volume},dst=/config",
                "--mount", f"type=bind,src={root / 'server'},dst=/input,readonly", "--entrypoint", "sh", ref,
                "-ec", "cp /input/* /config/; chmod 0700 /config; chmod 0600 /config/*; chown -R 70:70 /config")
            if pg_image["Config"]["User"] != "70:70":
                raise ValueError("This physical-profile PostgreSQL must always start non-root")
            attempted.append(pg)
            docker("run", "-d", "--name", pg, "--label", "sova.qualification=" + run,
                "--network", network, "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,size=192m,uid=70,gid=70,mode=0700",
                "--mount", f"type=volume,src={pg_volume},dst=/run/sova-tls,readonly",
                "--env", "POSTGRES_PASSWORD_FILE=/run/sova-tls/admin-password", "--env", "POSTGRES_DB=orders_source",
                args.postgres_image, "postgres",
                "-c", "ssl=on", "-c", "ssl_cert_file=/run/sova-tls/server.pem",
                "-c", "ssl_key_file=/run/sova-tls/server-key.pem",
                "-c", "hba_file=/run/sova-tls/pg_hba.conf", "-c", "password_encryption=scram-sha-256")
            until = time.monotonic() + 45
            while True:
                try:
                    # The entrypoint's temporary init server is socket-only.
                    # Wait for the real TCP server, not that short-lived phase.
                    docker("exec", pg, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "orders_source")
                    break
                except RuntimeError:
                    if time.monotonic() >= until:
                        raise RuntimeError("Disposable SCRAM database did not start") from None
                    time.sleep(0.2)
            password = secrets.token_urlsafe(40)
            # The generated password is URL-safe; still escape as an SQL literal.
            literal = "'" + password.replace("'", "''") + "'"
            statements = (f"CREATE ROLE orders_app LOGIN PASSWORD {literal};\n"
                "CREATE DATABASE other_tenant; REVOKE CONNECT ON DATABASE other_tenant FROM PUBLIC;\n"
                + SCHEMA + "\nGRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO orders_app;\n"
                "GRANT USAGE,SELECT ON SEQUENCE order_number_seq TO orders_app;\n")
            docker("exec", "-i", pg, "psql", "-X", "-U", "postgres", "-d", "orders_source",
                   "--set", "ON_ERROR_STOP=1", data=statements)
            ip = next(iter(json.loads(docker("inspect", pg))[0]["NetworkSettings"]["Networks"].values()))["IPAddress"]
            mounted = root / "mounted"
            mounted.mkdir(mode=0o700)
            (mounted / "database-ca.pem").write_bytes((root / "ca.pem").read_bytes())
            (mounted / "untrusted-ca.pem").write_bytes((root / "untrusted/ca.pem").read_bytes())
            (mounted / "database.dsn").write_text(
                f"host=localhost hostaddr={ip} port=5432 dbname=orders_source user=orders_app "
                f"password={password} sslmode=verify-full sslrootcert=/run/sova/database-ca.pem connect_timeout=2")
            (mounted / "client.token").write_text(secrets.token_urlsafe(40))
            (mounted / "config.json").write_text(json.dumps({"version": 1,
                "tenant_id": "tenant-canary", "application_id": "orders-canary", "run_id": run,
                "side": "source", "deployment_digest": ref,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}))
            docker("volume", "create", "--label", "sova.qualification=" + run, volume)
            created_volumes.append(volume)
            attempted.append(init)
            docker("run", "--name", init, "--label", "sova.qualification=" + run,
                "--network", "none", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                "--cap-add", "DAC_OVERRIDE",
                "--security-opt", "no-new-privileges", "--mount", f"type=volume,src={volume},dst=/config",
                "--mount", f"type=bind,src={mounted},dst=/input,readonly", "--entrypoint", "sh", ref,
                "-ec", "cp /input/* /config/; chmod 0700 /config; chmod 0600 /config/*; chown -R 10001:10001 /config")
            attempted.append(app)
            docker("run", "-d", "--name", app, "--label", "sova.qualification=" + run,
                "--network", network, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", "256m", "--cpus", "1", "--pids-limit", "64",
                "--mount", f"type=volume,src={volume},dst=/run/sova,readonly", ref)

            client = '''import json,urllib.request,urllib.error
from pathlib import Path
root=Path('/run/sova');cfg=json.loads((root/'config.json').read_text())
headers={'authorization':'Bearer '+(root/'client.token').read_text(),'x-qualification-tenant':cfg['tenant_id'],'x-qualification-run':cfg['run_id'],'idempotency-key':'canary-business-1','content-type':'application/json'}
def request(path='/orders',data=None,override=None):
 h={**headers,**(override or {})};r=urllib.request.Request('http://127.0.0.1:8080'+path,data=data,headers=h)
 try:
  with urllib.request.urlopen(r,timeout=5) as response:return response.status,json.load(response)
 except urllib.error.HTTPError as e:return e.code,json.load(e)
'''

            def check(name, expression):
                output = docker("exec", "-i", app, "python", "-", data=client + expression)
                report["tests"][name] = json.loads(output)
                if report["tests"][name] is not True:
                    raise RuntimeError("Real canary check failed: " + name)

            until = time.monotonic() + 20
            while True:
                try:
                    docker("exec", app, "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=1).read(2048)")
                    break
                except RuntimeError:
                    if time.monotonic() >= until:
                        raise RuntimeError("Non-root canary did not start") from None
                    time.sleep(0.2)
            check("unauthorized_denied", "print(json.dumps(request(override={'authorization':''})[0]==401))")
            check("wrong_scope_denied", "print(json.dumps(request(override={'x-qualification-tenant':'another'})[0]==403))")
            check("actual_order_and_idempotent_replay", """data=json.dumps({'customer':'canary-customer','amount':'12.50'}).encode()
a=request(data=data);b=request(data=data);c=request('/orders?customer=canary-customer&idempotency_key=canary-business-1')
print(json.dumps(a[0]==b[0]==c[0]==200 and a[1]==b[1]==c[1]))""")
            counts = docker("exec", pg, "psql", "-XAt", "-U", "postgres", "-d", "orders_source",
                "-c", "SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),"
                "(SELECT count(*) FROM idempotency_results)")
            report["tests"]["one_committed_business_effect_per_replayed_key"] = counts == "1|1|1"
            probes = '''import json,psycopg
from pathlib import Path
dsn=Path('/run/sova/database.dsn').read_text()
def denied(**kwargs):
 try:
  with psycopg.connect(dsn,**kwargs):pass
 except psycopg.Error:return True
 return False
with psycopg.connect(dsn) as c:
 tls=c.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()[0]
 result={'verified_tls_scram':tls is True,'wrong_ca_denied':denied(sslrootcert='/run/sova/untrusted-ca.pem'),'wrong_hostname_denied':denied(host='unregistered.invalid'),'wrong_password_denied':denied(password='incorrect-canary-password'),'plaintext_denied':denied(sslmode='disable'),'other_tenant_database_denied':denied(dbname='other_tenant')}
print(json.dumps(result))
'''
            report["tests"].update(json.loads(docker("exec", "-i", app, "python", "-", data=probes)))
            if not all(report["tests"].values()):
                raise RuntimeError("A real authentication/TLS boundary did not reject access")
            docker("exec", "-i", pg, "psql", "-X", "-U", "postgres", "-d", "orders_source",
                   "--set", "ON_ERROR_STOP=1", data="GRANT pg_monitor TO orders_app;\n")
            check("unexpected_role_membership_denied", "print(json.dumps(request('/orders?customer=canary-customer&idempotency_key=canary-business-1')[0]==503))")
            docker("exec", "-i", pg, "psql", "-X", "-U", "postgres", "-d", "orders_source",
                   "--set", "ON_ERROR_STOP=1", data="REVOKE pg_monitor FROM orders_app; ALTER ROLE orders_app NOLOGIN;\n")
            check("writer_fenced_new_connection_denied", "print(json.dumps(request('/orders?customer=canary-customer&idempotency_key=canary-business-1')[0]==503))")
            actual = json.loads(docker("inspect", app))[0]
            report["tests"]["nonroot_readonly_no_capabilities"] = (
                actual["Config"]["User"] == "10001:10001" and actual["HostConfig"]["ReadonlyRootfs"]
                and actual["HostConfig"]["CapDrop"] == ["ALL"])
            report["network_internal"] = json.loads(docker("network", "inspect", network))[0]["Internal"]
            passed = all(report["tests"].values()) and report["network_internal"]
    finally:
        if not passed:
            # Preserve bounded private startup evidence BEFORE removing the
            # owned containers. Never dump Env/Args/credentials into a report.
            for name in attempted:
                try:
                    actual = json.loads(docker("inspect", name))[0]
                except RuntimeError:
                    continue
                if actual["Config"].get("Labels", {}).get("sova.qualification") != run:
                    raise RuntimeError("Diagnostic target ownership changed")
                try:
                    state = actual.get("State", {})
                    projection = {key: state.get(key) for key in ("Status", "Running", "ExitCode", "OOMKilled", "StartedAt", "FinishedAt")}
                    (args.artifacts / (name + "-state.json")).write_text(json.dumps(projection, indent=2)+"\n")
                    logs = subprocess.run(["docker", "logs", "--tail", "100", name],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
                    (args.artifacts / (name + "-private.log")).write_bytes(logs.stdout[-65536:])
                except (OSError, subprocess.TimeoutExpired) as error:
                    report.setdefault("diagnostic_failures", []).append({"container": name, "kind": type(error).__name__})
        # Only exact names whose labels prove this disposable run owns them.
        for name in reversed(attempted):
            try:
                actual = json.loads(docker("inspect", name))[0]
            except RuntimeError:
                continue
            if actual["Config"].get("Labels", {}).get("sova.qualification") != run:
                raise RuntimeError("Refusing to remove an unowned container")
            docker("rm", "--force", name)
        for name in reversed(created_volumes):
            actual = json.loads(docker("volume", "inspect", name))[0]
            if actual.get("Labels", {}).get("sova.qualification") != run:
                raise RuntimeError("Refusing to remove an unowned volume")
            docker("volume", "rm", name)
        if created_network:
            actual = json.loads(docker("network", "inspect", network))[0]
            if actual.get("Labels", {}).get("sova.qualification") != run:
                raise RuntimeError("Refusing to remove an unowned network")
            docker("network", "rm", network)
        report["passed"] = passed
        report["containers_removed"] = all(not docker("ps", "-aq", "--filter", "name=^/" + name + "$")
                                           for name in attempted)
        report["network_removed"] = not docker("network", "ls", "-q", "--filter", "name=^" + network + "$")
        report["volumes_removed"] = all(not docker("volume", "ls", "-q", "--filter", "name=^" + name + "$")
                                       for name in created_volumes)
        (args.artifacts / "run.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": passed, "checks": len(report["tests"]), "containers_removed": report["containers_removed"]}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
