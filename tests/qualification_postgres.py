"""Run MQ-02/MQ-03 against a run-owned PostgreSQL container with verified TLS.

Uses an explicitly selected cached image ID; never pulls, resets the shared
kind environment, changes kubeconfig, or touches a cloud account. All database
state and certificates are disposable. This is isolation for testing, not a
production PostgreSQL deployment. Test roles share a run-owned TLS client
certificate; individual customer workload authentication is not qualified.
"""

import argparse
from datetime import UTC, datetime, timedelta
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import psycopg
from psycopg.conninfo import make_conninfo


def certificates(directory):
    now = datetime.now(UTC)
    server_input = directory / "server"
    server_input.mkdir(mode=0o700)
    ca_key, server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sova MQ-02 disposable test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    server = (x509.CertificateBuilder()
              .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
              .issuer_name(ca_name).public_key(server_key.public_key())
              .serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .add_extension(x509.SubjectAlternativeName([
                  x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
    client_key = ec.generate_private_key(ec.SECP256R1())
    client = (x509.CertificateBuilder()
              .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mq02-client")]))
              .issuer_name(ca_name).public_key(client_key.public_key())
              .serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    (directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (server_input / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (server_input / "server.pem").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (directory / "client.pem").write_bytes(client.public_bytes(serialization.Encoding.PEM))
    for name, key in (("server-key.pem", server_key), ("client-key.pem", client_key)):
        dest = server_input if name.startswith("server") else directory
        fd = os.open(dest / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    (server_input / "pg_hba.conf").write_text(
        "local all all trust\nhostssl all all 0.0.0.0/0 trust clientcert=verify-ca\n"
        "hostssl all all ::0/0 trust clientcert=verify-ca\n")


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE).strip()


class ClockWatch:
    """Observe host/DB drift without changing either clock or validity rules."""

    def __init__(self, dsn):
        self.dsn, self.samples = dsn, []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def sample(self):
        before = datetime.now(UTC)
        with psycopg.connect(self.dsn) as conn:
            database = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        after = datetime.now(UTC)
        # A DB timestamp within the real round trip needs no inferred offset.
        lower_bound = max(0.0, (before - database).total_seconds(), (database - after).total_seconds())
        self.samples.append({"host_before": before.isoformat(), "database": database.isoformat(),
                             "host_after": after.isoformat(), "skew_lower_bound_seconds": lower_bound})

    def watch(self):
        while not self.done.wait(0.5):
            try:
                self.sample()
            except psycopg.Error:
                self.samples.append({"error": "clock_sample_failed"})

    def start(self):
        self.sample()
        if self.samples[-1]["skew_lower_bound_seconds"] > 1:
            raise RuntimeError("Dedicated DB and host clocks differ by more than one second")
        self.thread.start()

    def stop(self):
        self.done.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        self.sample()
        return {"max_allowed_skew_seconds": 1, "samples": self.samples,
                "passed": all("error" not in s and s["skew_lower_bound_seconds"] <= 1
                              for s in self.samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Full cached sha256 image ID; no tag or automatic pull")
    parser.add_argument("--security-source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True, help="New private output directory")
    parser.add_argument("--suite", choices=("requests", "service", "runtime", "worker", "startup", "pods"), default="requests",
                        help="MQ-02, service, runtime API, or worker process/fault qualification checks")
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        parser.error("An exact cached image ID is required")
    if not (args.security_source / "sova_security").is_dir():
        parser.error("The pinned security source's src directory is required")
    root = Path(__file__).resolve().parents[1]
    args.artifacts.mkdir(parents=True, mode=0o700, exist_ok=False)
    run = {"requests": "mq02-", "service": "mq03-", "runtime": "mq03-runtime-",
           "worker": "mq04-worker-", "startup": "worker-startup-", "pods": "worker-pods-"}[args.suite] + uuid4().hex
    container, network = "sova-" + run, "sova-network-" + run
    report = {"run_id": run, "image_id": args.image, "shared_environment_used": False,
              "qualification_issued": False, "network_created": False, "container_created": False,
              "suite": args.suite}
    # The worker regression fixtures leave their many tiny databases for the
    # run-owned container to dispose of. Account for template DBs and WAL, not
    # only business rows; never reset a shared database to make room.
    data_mib, memory_mib = (1024, 1536) if args.suite in ("worker", "startup") else (384, 512)
    report.update(data_tmpfs_mib=data_mib, container_memory_mib=memory_mib)
    report["source_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    report["security_source_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=args.security_source, text=True).strip()
    if report["security_source_commit"] != "27e6e85514a7cbb3e43d216464b72d9bd78f3fcf":
        parser.error("The security producer differs from the pinned qualification source")
    measured_sources = [*sorted((root / "src/sova/qualification").glob("*.py")),
                        root / "tests/unit/test_request_qualification.py",
                        root / "tests/integration/test_order_http_handoff.py",
                        root / "tests/qualification_orders.py", Path(__file__).resolve(),
                        root / "examples/qualification/orders-http-postgres-v1.json"]
    tests = ["tests/unit/test_request_qualification.py", "tests/integration/test_order_http_handoff.py"]
    if args.suite in ("service", "runtime", "worker", "startup"):
        tests.extend(("tests/integration/test_service_api.py",
                      "tests/integration/test_service_control_plane.py",
                      "tests/integration/test_order_service_journey.py",
                      "tests/integration/test_service_worker.py"))
        measured_sources.extend([*sorted((root / "src/sova/service").glob("*.py")),
                                 root / "tests/qualification_service.py",
                                 root / "tests/contract_world.py", root / "tests/control_plane_support.py",
                                 *(root / test for test in tests[2:])])
    if args.suite in ("runtime", "worker", "startup"):
        tests.append("tests/integration/test_service_runtime_journey.py")
        measured_sources.extend([root / "tests/qualification_runtime.py", root / tests[-1]])
    if args.suite in ("worker", "startup"):
        tests.append("tests/integration/test_worker_process_journey.py")
        measured_sources.extend([root / "tests/qualification_worker.py", root / tests[-1]])
    if args.suite == "startup":
        startup_tests = ["tests/unit/test_worker_startup.py",
                         "tests/integration/test_worker_startup_postgres.py"]
        startup_tests.extend("tests/unit/test_service_deployment.py::" + name for name in (
            "test_secrets_are_read_from_mounted_files_never_from_the_environment",
            "test_the_jwks_is_fetched_over_https_except_for_a_loopback_test_issuer",
            "test_a_workers_owner_id_names_its_pod_and_is_new_at_every_start",
            "test_owner_ids_that_name_no_pod_are_never_proved_gone"))
        tests.extend(startup_tests)
        measured_sources.extend(root / test.split("::", 1)[0] for test in startup_tests)
    if args.suite == "pods":
        tests = ["tests/integration/test_worker_pod_journey.py"]
        measured_sources.extend([root / "tests/qualification_pods.py", root / tests[0],
                                 root / "tests/qualification_service.py", root / "tests/contract_world.py",
                                 root / "tests/control_plane_support.py", root / "tests/postgres_fixtures.py",
                                 root / "tests/postgres_business.py", root / "tests/handoff_drive.py",
                                 *sorted((root / "src/sova/service").glob("*.py"))])
    report["test_files"] = tests
    report["working_tree_sources_sha256"] = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in measured_sources}
    network_created = container_attempted = False

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    code = 1
    try:
        info = json.loads(docker("image", "inspect", args.image))[0]
        report["architecture"] = info["Architecture"]
        with tempfile.TemporaryDirectory(prefix="sova-mq02-tls-") as temp:
            tls = Path(temp)
            certificates(tls)
            docker("network", "create", "--label", "sova.qualification=" + run, network)
            network_created = report["network_created"] = True
            report["network_mode"] = "run-owned bridge; published on loopback only"
            report["client_authentication"] = "run-owned TLS certificate shared by fixture roles"
            wrapper = ("install -d -m 0700 -o postgres -g postgres /run/sova-tls; "
                       "cp /sova-input/ca.pem /sova-input/server.pem /sova-input/server-key.pem "
                       "/sova-input/pg_hba.conf /run/sova-tls/; "
                       "chown postgres:postgres /run/sova-tls/*; "
                       "chmod 0600 /run/sova-tls/server-key.pem; "
                       'exec docker-entrypoint.sh "$@"')
            container_attempted = True
            docker("run", "--detach", "--name", container, "--label", "sova.qualification=" + run,
                   "--network", network, "--publish", "127.0.0.1::5432", "--memory", f"{memory_mib}m", "--cpus", "1",
                   "--pids-limit", "128", "--security-opt", "no-new-privileges",
                   "--tmpfs", f"/var/lib/postgresql/data:rw,nosuid,size={data_mib}m",
                   "--tmpfs", "/run/sova-tls:rw,noexec,nosuid,size=1m",
                   "--mount", f"type=bind,src={tls / 'server'},dst=/sova-input,readonly",
                   "--env", "POSTGRES_DB=sova_test", "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
                   "--entrypoint", "sh", args.image, "-ec", wrapper, "qualification",
                   "postgres", "-c", "cluster_name=sova-persistence-test", "-c", "ssl=on",
                   "-c", "ssl_cert_file=/run/sova-tls/server.pem",
                   "-c", "ssl_key_file=/run/sova-tls/server-key.pem",
                   "-c", "ssl_ca_file=/run/sova-tls/ca.pem", "-c", "hba_file=/run/sova-tls/pg_hba.conf",
                   "-c", "wal_level=logical", "-c", "max_prepared_transactions=10")
            report["container_created"] = True
            port = docker("port", container, "5432/tcp")
            if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", port):
                raise RuntimeError("The test database must bind one loopback address")
            dsn = make_conninfo(host="localhost", hostaddr="127.0.0.1", port=port.split(":")[1],
                                user="postgres", dbname="sova_test", connect_timeout=2,
                                sslmode="verify-full", sslrootcert=str(tls / "ca.pem"),
                                sslcert=str(tls / "client.pem"), sslkey=str(tls / "client-key.pem"))
            deadline = time.monotonic() + 45
            while True:
                try:
                    with psycopg.connect(dsn) as conn:
                        identity = conn.execute("SELECT current_setting('cluster_name'),current_database(),"
                                                "current_setting('server_version'),"
                                                "(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid())").fetchone()
                        if identity[:2] != ("sova-persistence-test", "sova_test") or identity[3] is not True:
                            raise RuntimeError("Wrong database identity or TLS")
                        report["postgres_version"], report["verified_tls"] = identity[2], True
                        break
                except psycopg.OperationalError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Dedicated PostgreSQL did not become ready") from None
                    time.sleep(0.2)
            try:
                with psycopg.connect(dsn, sslcert="", sslkey=""):
                    raise RuntimeError("A client without the run certificate was accepted")
            except psycopg.OperationalError:
                report["missing_client_certificate_denied"] = True
            with psycopg.connect(dsn) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")}
            env.update(PYTHONPATH=str(root / "src") + os.pathsep + str(args.security_source.resolve()),
                       AWS_EC2_METADATA_DISABLED="true", SOVA_TEST_POSTGRES_DSN=dsn,
                       SOVA_TEST_QUALIFICATION_ARTIFACTS=str(args.artifacts.resolve()))
            if args.suite == "pods":
                env.update(SOVA_TEST_QUALIFICATION_RUN=run,
                           SOVA_TEST_QUALIFICATION_DOCKER_NETWORK=network,
                           SOVA_TEST_QUALIFICATION_PG_CONTAINER=container,
                           SOVA_TEST_QUALIFICATION_SECURITY_SOURCE=str(args.security_source.resolve().parent))
            clocks = ClockWatch(dsn)
            try:
                clocks.start()
                code = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                       *(["-s"] if args.suite == "pods" else []), *tests,
                                       "--junitxml=" + str(args.artifacts.resolve() / "results.xml")],
                                      cwd=root, env=env, check=False).returncode
                report["pytest_exit_code"] = code
                report["database_filesystem_usage"] = docker(
                    "exec", container, "df", "-k", "/var/lib/postgresql/data").splitlines()
                if code:
                    logs = subprocess.run(["docker", "logs", "--tail", "200", container],
                                          capture_output=True, text=True)
                    (args.artifacts / "postgres-test-failure.log").write_text(logs.stdout + logs.stderr)
            finally:
                report["clock_checks"] = clocks.stop()
            if not report["clock_checks"]["passed"]:
                report["environment_error"] = "host_database_clock_skew_or_sample_failure"
                code = 1
    except BaseException as error:
        report["failure_type"] = type(error).__name__
        if container_attempted:
            state = subprocess.run(["docker", "inspect", container], capture_output=True, text=True)
            if state.returncode == 0:
                actual = json.loads(state.stdout)[0]
                report["container_state"] = {k: actual["State"][k] for k in ("Status", "ExitCode", "OOMKilled")}
                report["port_bindings"] = actual["NetworkSettings"].get("Ports")
            logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
            (args.artifacts / "postgres-setup.log").write_text(logs.stdout + logs.stderr)
        raise
    finally:
        # Names alone are not authority to delete anything.
        if container_attempted:
            found = subprocess.run(["docker", "inspect", container], text=True, capture_output=True)
            if found.returncode == 0:
                actual = json.loads(found.stdout)[0]
                if actual["Config"].get("Labels", {}).get("sova.qualification") != run:
                    raise RuntimeError("Refusing to remove an unowned container")
                docker("rm", "--force", container)
                report["container_removed"] = True
        if network_created:
            actual = json.loads(docker("network", "inspect", network))[0]
            if actual.get("Labels", {}).get("sova.qualification") != run:
                raise RuntimeError("Refusing to remove an unowned network")
            docker("network", "rm", network)
            report["network_removed"] = True
        (args.artifacts / "run.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
