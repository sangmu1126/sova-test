"""The control plane's processes, one command each.

    sova-service migrate     apply the schema and the runtime role's grants (a Job,
                             run before each rollout, as the schema owner)
    sova-service serve-api   the HTTP API (a Deployment, any number of replicas)
    sova-service run-worker  a handoff worker (a Deployment, any number of replicas)

Every replica is stateless: the database holds registrations, plans,
authority, leases and the effect ledger; the authority witness is shared.
Configuration is read from the environment and mounted secret files
(``config.py``). Logs are JSON lines on stdout.

SIGTERM: the API stops accepting connections and finishes requests in
flight; a worker finishes its current step, releases its lease and exits, so
another replica continues the handoff at the next step without recovery.
"""

import argparse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import signal
import sys
import threading
import time

log = logging.getLogger("sova.service")


class _JsonLines(logging.Formatter):
    def format(self, record):
        entry = {"ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
                 "level": record.levelname, "logger": record.name,
                 "msg": record.getMessage()}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(entry, ensure_ascii=False)


def _logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonLines())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("SOVA_LOG_LEVEL", "INFO"))


def _clock():
    return datetime.now(UTC)


def _connect(config):
    import psycopg

    return lambda: psycopg.connect(config.database_url, connect_timeout=10)


def _settings(config):
    from sova.service.control_plane import Settings

    return Settings(registry_verifier_version=config.registry_verifier_version,
                    evidence_verifier=config.evidence_verifier,
                    evidence_issuer=config.evidence_issuer,
                    witness_for=config.witness_for())


def schema_ready(connect):
    """True when every migration this code expects is applied."""
    from sova.service import schema

    with connect() as conn:
        applied = {r[0] for r in conn.execute(
            "SELECT name FROM sova_control.schema_migrations")}
    return set(schema.MIGRATIONS) <= applied


def migrate(env=os.environ):
    import psycopg

    from sova.service import schema
    from sova.service.config import _required, _secret

    dsn, role = _secret("SOVA_MIGRATION_DATABASE_URL_FILE", env), _required(
        "SOVA_RUNTIME_ROLE", env)
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        applied = schema.apply(conn)
        schema.grant_runtime(conn, role)
    log.info("schema current; applied now: %s", ", ".join(applied) or "nothing")
    return 0


def serve_api(env=os.environ, *, host="0.0.0.0", port=8080):
    import httpx
    import uvicorn

    from sova.service.api import create_app
    from sova.service.auth import AuthenticationUnavailable, BearerAuthenticator
    from sova.service.config import ServiceConfig
    from sova.service.control_plane import ControlPlane

    config = ServiceConfig.from_env(env)
    connect = _connect(config)

    def fetch_jwks():
        try:
            response = httpx.get(config.jwks_url, timeout=5.0, follow_redirects=False)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            # Do not turn a provider/TLS failure into a valid principal or leak
            # a URL, proxy response or certificate path to the caller.
            raise AuthenticationUnavailable("Identity provider unavailable") from None

    authenticate = BearerAuthenticator(
        issuer=config.issuer, audience=config.audience, fetch_jwks=fetch_jwks,
        tenant_claim=config.tenant_claim, roles_claim=config.roles_claim,
        role_prefix=config.role_prefix)
    plane = ControlPlane(connect, settings=_settings(config), clock=_clock)
    app = create_app(plane, authenticate, ready=lambda: schema_ready(connect))
    if env.get("SOVA_CONSOLE_ROOT"):
        from sova.service.web import mount_workspace
        from sova.service.config import _required
        mount_workspace(app,root=env["SOVA_CONSOLE_ROOT"],
            origin=_required("SOVA_CONSOLE_ORIGIN",env),issuer=config.issuer,
            client_id=_required("SOVA_CONSOLE_CLIENT_ID",env),
            tenant_id=_required("SOVA_CONSOLE_TENANT",env),audience=config.audience)
    # uvicorn handles SIGTERM: stop accepting, drain requests in flight.
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False,
                proxy_headers=False, server_header=False, timeout_graceful_shutdown=25)
    return 0


def _health_server(worker, stall_seconds, port, *, host="0.0.0.0"):
    """Liveness for a worker: fails once it has made no progress (a poll or
    a step boundary) for ``stall_seconds``, so a hung worker is restarted in
    place and its successor recovers the step it held."""

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            stalled = time.monotonic() - worker.progressed > stall_seconds
            ok = self.path == "/healthz" and not stalled
            self.send_response(200 if ok else 503)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok" if ok else "stalled"}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Health)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server


def run_worker(env=os.environ, *, health_port=8081, poll_seconds=5.0):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
    from sova.domain.contracts import ContractError
    from sova.service.catalog import catalog_from
    from sova.service.config import LedgerConfig, WorkerConfig
    from sova.service.quiescence import KubernetesWorkerQuiescence, in_cluster_pod_state
    from sova.service.worker import HandoffWorker

    if type(health_port) is not int or not 1 <= health_port <= 65535:
        raise ContractError("Worker health_port must be an integer in 1..65535")
    config, worker_config = LedgerConfig.from_env(env), WorkerConfig.from_env(env)
    if type(poll_seconds) not in (int, float) or not 0 < poll_seconds < worker_config.stall_seconds:
        raise ContractError("Worker poll_seconds must be positive and below the stall timeout")
    if worker_config.execution.get("version") == 2:
        from sova.service.directional_catalog import catalog_v2

        catalog, close = catalog_v2(worker_config.execution)
    else:
        catalog, close = catalog_from(worker_config.execution)
    signer = Ed25519EvidenceSigner(
        Ed25519PrivateKey.from_private_bytes(worker_config.evidence_key),
        issuer_id=config.evidence_issuer, key_id=worker_config.evidence_key_id)
    owner = worker_config.owner_id
    # A dead local process does not prove its remote DB request has settled.
    # Host workers therefore retain the existing manual UNKNOWN/hold path.
    quiescence = None
    if worker_config.identity_mode == "kubernetes":
        quiescence = KubernetesWorkerQuiescence(
            pod_running=in_cluster_pod_state(worker_config.pod_namespace), clock=_clock,
            this_owner=owner)
    worker = HandoffWorker(_connect(config), settings=_settings(config), catalog=catalog,
                           signer=signer, clock=_clock, owner_id=owner,
                           quiescence=quiescence, lease_seconds=worker_config.lease_seconds,
                           max_step_seconds=worker_config.stall_seconds)

    def stop(signum, frame):
        log.info("signal %s: finishing the current step, then exiting", signum)
        worker.stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    health = _health_server(
        worker, worker_config.stall_seconds, health_port,
        host="127.0.0.1" if worker_config.identity_mode == "host" else "0.0.0.0")
    log.info("worker %s serving %s", owner, ", ".join(worker_config.tenants))
    try:
        worker.run(worker_config.tenants, poll_seconds=poll_seconds,
                   ready=lambda: schema_ready(worker.connect))
    finally:
        health.shutdown()
        close()
    log.info("worker %s stopped", owner)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sova-service",
                                     description="Sova control plane processes")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Apply the schema and the runtime grants")
    api = commands.add_parser("serve-api", help="Serve the HTTP API")
    api.add_argument("--host", default="0.0.0.0", help="Listen address; use 127.0.0.1 for host-local tests")
    api.add_argument("--port", type=int, default=8080)
    worker = commands.add_parser("run-worker", help="Run a handoff worker")
    worker.add_argument("--health-port", type=int, default=8081)
    worker.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    _logging()
    try:
        if args.command == "migrate":
            return migrate()
        if args.command == "serve-api":
            return serve_api(host=args.host, port=args.port)
        return run_worker(health_port=args.health_port, poll_seconds=args.poll_seconds)
    except Exception:
        log.exception("%s failed", args.command)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
