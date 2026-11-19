"""Trusted source-side image trial; fresh data/credentials, no external network.

Executed by SSM on the exact owned instance. No secret appears in the SSM
program: fresh inputs arrive through one version-pinned private S3 object.
Neither the existing source containers nor their data/route are changed.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def main():
    os.umask(0o077)
    c = json.load(sys.stdin)
    if Path(
        "/var/lib/sova-journey/run-id"
    ).read_text().strip() != "b0be270e62" or not re.fullmatch(
        "[a-f0-9]{12}", c["suffix"]
    ):
        raise ValueError("Wrong owned source trial")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(c["expires_at"]):
        raise ValueError("Expired source trial")
    available = int(
        next(
            line.split()[1]
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
    )
    if available < 1024 * 1024:
        raise ValueError("Insufficient available memory for the isolated source trial")
    root = Path("/run/sova-image-" + c["suffix"])
    if (
        json.loads(subprocess.check_output(["findmnt", "-J", "-T", "/run"]))[
            "filesystems"
        ][0]["fstype"]
        != "tmpfs"
    ):
        raise ValueError("Private trial inputs require tmpfs")
    root.mkdir(mode=0o700, exist_ok=False)
    ns = "sova-image-" + c["suffix"]
    base = ["/usr/local/bin/nerdctl", "--namespace", ns]
    names = {"app": ns + "-app", "db": ns + "-db"}
    report = {
        "run_id": "b0be270e62",
        "trial_id": ns,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "checks": {},
        "passed": False,
        "existing_application_changed": False,
        "external_network": False,
        "operational_qualification": False,
        "migration_complete": False,
    }

    def call(*args, data=None, check=True):
        p = subprocess.run(args, input=data, capture_output=True, timeout=45)
        if check and p.returncode:
            raise RuntimeError("Bounded source trial command failed: " + args[0])
        return p

    def snapshot_original():
        old = ["/usr/local/bin/nerdctl", "--namespace", "sova-b0be270e62"]
        rows = (
            call(
                *old,
                "exec",
                "orders-db",
                "psql",
                "-XAt",
                "-U",
                "postgres",
                "-d",
                "source_orders",
                "-c",
                "SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)",
            )
            .stdout.decode()
            .strip()
        )
        containers = json.loads(call(*old, "inspect", "orders-app", "orders-db").stdout)
        return {
            "rows": rows,
            "containers": [{k: x[k] for k in ("Id", "Image")} for x in containers],
        }

    before = snapshot_original()
    if before["rows"] != "1|1|1":
        raise ValueError("Original source is not the idle owned canary")
    cleanup = (
        """import json,pathlib,subprocess
ns=@NS@;names=@NAMES@;base=['/usr/local/bin/nerdctl','--namespace',ns]
for name in names:
 p=subprocess.run(base+['inspect',name],capture_output=True,timeout=15)
 if p.returncode:continue
 x=json.loads(p.stdout)[0]
 if x['Config'].get('Labels',{}).get('sova.image-probe')!=@SUFFIX@:raise ValueError('Foreign trial container')
 if x['State'].get('Running'):subprocess.run(base+['stop','--time','8',name],capture_output=True,check=True,timeout=20)
 subprocess.run(base+['rm',name],capture_output=True,check=True,timeout=15)
root=pathlib.Path('/run')/ns
for folder in (root/'app',root/'db'):
 if folder.exists():
  if folder.is_symlink():raise ValueError('Unexpected trial directory link')
  for p in folder.iterdir():
   if p.is_symlink() or not p.is_file():raise ValueError('Unexpected trial input entry')
   p.unlink()
  folder.rmdir()
for name in ('bundle.json','app.oci.tar','db.oci.tar'):
 p=root/name
 if p.is_symlink():raise ValueError('Unexpected trial input link')
 if p.exists():p.unlink()
""".replace("@NS@", repr(ns))
        .replace("@NAMES@", repr([names["app"], names["db"]]))
        .replace("@SUFFIX@", repr(c["suffix"]))
    )
    (root / "cleanup.py").write_text(cleanup)
    call(
        "systemd-run",
        "--unit=sova-probe-expiry-" + c["suffix"],
        "--on-active=5m",
        "--property=Type=oneshot",
        "/usr/bin/python3",
        str(root / "cleanup.py"),
    )
    try:
        bundle = root / "bundle.json"
        fetched = call(
            "aws",
            "s3api",
            "get-object",
            "--bucket",
            c["bucket"],
            "--key",
            c["input_key"],
            "--if-match",
            c["input_etag"],
            str(bundle),
            "--region",
            "ap-northeast-2",
        )
        metadata = json.loads(fetched.stdout)
        if (
            metadata.get("VersionId") != c["input_version"]
            or metadata.get("ServerSideEncryption") != "AES256"
        ):
            raise ValueError(
                "Conditional input fetch returned a different version or encryption"
            )
        raw = bundle.read_bytes()
        if hashlib.sha256(raw).hexdigest() != c["input_sha256"] or len(raw) > 65536:
            raise ValueError("Version-pinned trial inputs differ")
        payload = json.loads(raw)
        if set(payload) != {"app", "db", "role_sql"}:
            raise ValueError("Unexpected trial input schema")
        expected_files = {
            "db": {"server.pem", "server-key.pem", "ca.pem", "admin-password", "pg_hba.conf"},
            "app": {"server.pem", "server-key.pem", "ca.pem", "database-ca.pem", "untrusted-ca.pem", "database.dsn", "client.token", "config.json"},
        }
        for kind, uid in (("app", 10001), ("db", 70)):
            if set(payload[kind]) != expected_files[kind]:
                raise ValueError("Unexpected trial input files")
            folder = root / kind
            folder.mkdir(mode=0o700)
            for name, value in payload[kind].items():
                p = folder / name
                p.write_text(value)
                p.chmod(0o600)
                os.chown(p, uid, uid)
            os.chown(folder, uid, uid)
        refs = {}
        for kind, proof in c["images"].items():
            archive = root / (kind + ".oci.tar")
            call(
                "aws",
                "s3",
                "cp",
                "s3://" + c["bucket"] + "/" + proof["key"],
                str(archive),
                "--region",
                "ap-northeast-2",
                "--only-show-errors",
            )
            h = hashlib.sha256()
            with archive.open("rb") as f:
                while True:
                    b = f.read(1024 * 1024)
                    if not b:
                        break
                    h.update(b)
            if h.hexdigest() != proof["archive_sha256"]:
                raise ValueError("Source image archive differs")
            image = "local.sova.invalid/" + ns + "/" + kind
            call(
                "/usr/local/bin/ctr",
                "--namespace",
                ns,
                "images",
                "import",
                "--base-name",
                image,
                "--digests",
                str(archive),
            )
            for digest in (proof["manifest_digest"], proof["config_digest"]):
                observed = call(
                    "/usr/local/bin/ctr", "--namespace", ns, "content", "get", digest
                ).stdout
                if "sha256:" + hashlib.sha256(observed).hexdigest() != digest:
                    raise ValueError("Runtime content differs")
            refs[kind] = image + "@" + proof["manifest_digest"]
            archive.unlink()
        common = [
            "--label",
            "sova.image-probe=" + c["suffix"],
            "--label",
            "sova.run=b0be270e62",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--cpus",
            "0.5",
        ]
        db = names["db"]
        call(
            *base,
            "run",
            "-d",
            "--name",
            db,
            *common,
            "--network",
            "none",
            "--memory",
            "512m",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,nosuid,size=192m,uid=70,gid=70,mode=0700",
            "--mount",
            "type=bind,src=" + str(root / "db") + ",dst=/run/input,ro",
            "--env",
            "POSTGRES_PASSWORD_FILE=/run/input/admin-password",
            "--env",
            "POSTGRES_DB=orders_source",
            refs["db"],
            "postgres",
            "-c",
            "ssl=on",
            "-c",
            "ssl_cert_file=/run/input/server.pem",
            "-c",
            "ssl_key_file=/run/input/server-key.pem",
            "-c",
            "hba_file=/run/input/pg_hba.conf",
            "-c",
            "password_encryption=scram-sha-256",
        )
        until = time.monotonic() + 45
        while call(
            *base,
            "exec",
            db,
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            "orders_source",
            check=False,
        ).returncode:
            if time.monotonic() >= until:
                raise ValueError("Source trial PG startup deadline exceeded")
            time.sleep(0.2)
        call(
            *base,
            "exec",
            "-i",
            db,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "orders_source",
            data=payload["role_sql"].encode(),
        )
        app = names["app"]
        call(
            *base,
            "run",
            "-d",
            "--name",
            app,
            *common,
            "--network",
            "container:" + db,
            "--memory",
            "256m",
            "--read-only",
            "--mount",
            "type=bind,src=" + str(root / "app") + ",dst=/run/sova,ro",
            refs["app"],
            "python",
            "-m",
            "uvicorn",
            "sova.qualification.order_runtime:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8080",
            "--no-access-log",
            "--no-proxy-headers",
            "--no-server-header",
            "--ssl-certfile",
            "/run/sova/server.pem",
            "--ssl-keyfile",
            "/run/sova/server-key.pem",
        )
        health = "import urllib.request,ssl;urllib.request.urlopen('https://localhost:8080/healthz',context=ssl.create_default_context(cafile='/run/sova/ca.pem'),timeout=1).read(2048)"
        until = time.monotonic() + 20
        while call(*base, "exec", app, "python", "-c", health, check=False).returncode:
            if time.monotonic() >= until:
                raise ValueError("Source trial app startup deadline exceeded")
            time.sleep(0.2)
        # The caller supplies public verifier bytes separately, never code from
        # an AI output or the private input bundle.
        check_program = c["probe_program"]
        check_result = call(
            *base, "exec", "-i", app, "python", "-", data=check_program.encode()
        )
        result = json.loads(check_result.stdout)
        if not result or any(v is not True for v in result.values()):
            raise ValueError("Source image runtime checks failed")
        report["checks"] = result
        report["passed"] = True
    except Exception as error:
        report["failure_type"] = type(error).__name__
    finally:
        if not report["passed"]:
            for name in names.values():
                p = call(*base, "inspect", name, check=False)
                if p.returncode == 0:
                    x = json.loads(p.stdout)[0]
                    (root / (name + "-state.json")).write_text(
                        json.dumps(
                            {
                                k: x.get("State", {}).get(k)
                                for k in ("Status", "Running", "ExitCode", "OOMKilled")
                            }
                        )
                    )
                    (root / (name + "-private.log")).write_bytes(
                        call(*base, "logs", "--tail", "80", name, check=False).stdout[
                            -65536:
                        ]
                    )
        call("/usr/bin/python3", str(root / "cleanup.py"))
        after = snapshot_original()
        report["original_unchanged"] = before == after
        report["containers_removed"] = all(
            call(*base, "inspect", n, check=False).returncode != 0
            for n in names.values()
        )
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        # Keep report and public cleanup code, remove only fresh tmpfs secrets.
        for folder in (root / "app", root / "db"):
            if folder.exists():
                for p in folder.iterdir():
                    if p.is_file() and not p.is_symlink():
                        p.unlink()
                folder.rmdir()
        if (root / "bundle.json").exists():
            (root / "bundle.json").unlink()
        (root / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if not report["passed"] or not report["original_unchanged"] or not report["containers_removed"]:
        raise ValueError("Source image runtime or owned cleanup did not verify")


if __name__ == "__main__":
    main()
