"""MQ-03 runtime API process and HTTPS JWKS, isolated to one local test run.

The service runs its real CLI/configuration/connection factory. The IdP keys
and inventory are still fixtures; the worker is the existing in-process
worker, not a deployed daemon. No shared cluster or network policy is changed.
"""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

import httpx
from psycopg.conninfo import make_conninfo
from qualification_postgres import certificates
from qualification_service import ServiceJourney
from test_service_api import AUDIENCE, ISSUER

from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.service.control_plane import ControlPlane


def private_file(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(value)
    return path


class HttpsJwks:
    def __init__(self, directory, issuer):
        directory.mkdir(mode=0o700)
        certificates(directory)
        self.ca = directory / "ca.pem"
        self.gets = 0
        owner = self
        document = json.dumps(issuer.jwks()).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - standard HTTP server interface
                if self.path != "/jwks":
                    self.send_error(404)
                    return
                owner.gets += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(document)))
                self.end_headers()
                self.wfile.write(document)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(directory / "server/server.pem", directory / "server/server-key.pem")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = "https://127.0.0.1:" + str(self.server.server_address[1]) + "/jwks"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Run-owned HTTPS JWKS did not stop")


class RuntimeJourney(ServiceJourney):
    def __init__(self, postgres_database, business, directory):
        super().__init__(postgres_database, business, directory)
        witness_dir = directory / "witness"
        witness_dir.mkdir(mode=0o700)
        self.settings = replace(self.settings, witness_for=lambda tenant: FileAuthorityWitness(
            witness_dir / f"{tenant}.log", tenant_id=tenant))
        self.plane = ControlPlane(self.connect, settings=self.settings, clock=self.clock)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        dsn, role = postgres_database
        self.runtime_dsn = make_conninfo(dsn, user=role)
        self.dsn_file = private_file(directory / "runtime-dsn", self.runtime_dsn)
        self.directory = directory
        self.environment = {k: v for k, v in os.environ.items() if k in (
            "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH")}
        self.environment.update({
            "AWS_EC2_METADATA_DISABLED": "true", "SOVA_DATABASE_URL_FILE": str(self.dsn_file),
            "SOVA_OIDC_ISSUER": ISSUER, "SOVA_OIDC_AUDIENCE": AUDIENCE,
            "SOVA_REGISTRY_VERIFIER_VERSION": self.settings.registry_verifier_version,
            "SOVA_EVIDENCE_VERIFIER": self.settings.evidence_verifier,
            "SOVA_EVIDENCE_ISSUER": self.settings.evidence_issuer,
            "SOVA_WITNESS": "file:" + str(witness_dir)})
        self.process, self.output = None, None
        self.starts, self.exits = 0, []
        self.case_name = "service-handoff-and-failback"

    def start_api(self):
        self.starts += 1
        self.output = (self.directory / f"api-{self.starts}.log").open("x")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "sova.service.main", "serve-api", "--host", "127.0.0.1",
             "--port", str(self.port)], env=self.environment, stdout=self.output,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.client = httpx.Client(base_url=self.origin, timeout=15, trust_env=False,
                                   follow_redirects=False)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Run-owned API process exited during startup; inspect its private log")
            try:
                if self.client.get("/healthz").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        raise RuntimeError("Run-owned API did not become live")

    def stop_api(self):
        if self.client:
            self.client.close()
            self.client = None
        if self.process:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
            try:
                code = self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise RuntimeError("API required SIGKILL instead of graceful shutdown") from None
            finally:
                self.output.close()
            log_path = self.directory / f"api-{self.starts}.log"
            messages = [json.loads(line).get("msg") for line in log_path.read_text().splitlines()
                        if line.startswith("{")]
            shutdown = "Application shutdown complete." in messages
            finished = any(type(message) is str and message.startswith("Finished server process [")
                           for message in messages)
            self.exits.append({"exit_code": code, "application_shutdown_complete": shutdown,
                               "server_process_finished": finished, "forced_kill": False})
            # Some uvicorn versions re-raise the captured SIGTERM after draining
            # requests. A signal exit alone is never proof of graceful shutdown.
            artifacts = os.environ.get("SOVA_TEST_QUALIFICATION_ARTIFACTS")
            if artifacts:
                from pathlib import Path
                with (Path(artifacts) / f"runtime-{self.case_name}-api-{self.starts}.log").open("xb") as file:
                    file.write(log_path.read_bytes())
            self.process = None
            if code not in (0, -signal.SIGTERM) or not shutdown or not finished:
                raise RuntimeError("API exited unsuccessfully; inspect its private log")

    def restart_api(self):
        self.stop_api()
        self.start_api()

    def approve(self, compiled, approval):
        plan = super().approve(compiled, approval)
        # Admission must reread the persisted plan/request/approval after this
        # API process and its JWKS cache have gone away. No worker is running.
        self.restart_api()
        return plan

    def record(self, name, **data):
        super().record("runtime-" + name, **data, runtime={
            "api_starts": self.starts, "api_exits_observed": self.exits,
            "jwks_https_gets": self.jwks.gets, "database_profile_versions": self.database_versions,
            "all_api_processes_stopped": self.process is None,
            "api_transport": "loopback HTTP", "jwks_transport": "certificate-verified HTTPS",
            "cli": "python -m sova.service.main serve-api --host 127.0.0.1",
            "worker_deployed": False})


@contextmanager
def runtime_service_journey(postgres_database, business, directory, *, case_name=None,
                            wrong_jwks_ca=False, wrong_database_ca=False):
    h = RuntimeJourney(postgres_database, business, directory)
    if case_name:
        h.case_name = case_name
    with HttpsJwks(directory / "jwks", h.issuer) as jwks:
        h.jwks = jwks
        h.environment.update(SOVA_OIDC_JWKS_URL=jwks.url, SSL_CERT_FILE=str(jwks.ca))
        if wrong_jwks_ca or wrong_database_ca:
            bad = directory / "untrusted-ca"
            bad.mkdir(mode=0o700)
            certificates(bad)
            if wrong_jwks_ca:
                h.environment["SSL_CERT_FILE"] = str(bad / "ca.pem")
            if wrong_database_ca:
                h.dsn_file.write_text(make_conninfo(h.runtime_dsn, sslrootcert=str(bad / "ca.pem")))
        try:
            h.start_api()
            yield h
        finally:
            h.stop_api()
            h.record("cleanup-" + h.case_name)
