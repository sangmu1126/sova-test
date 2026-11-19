"""Real HTTP monitoring through the inspected upstream Collector.

Two private controller HTTP gateways fetch only the registered AWS/VM app
health endpoints. Their real SERVER spans (not app-server instrumentation)
use the official SDK, per-producer mTLS/token and independent OTLP projection.
Only sampled gateway latency is measured; no app SLO, total traffic, billing,
migration, production monitoring backend or geographical placement is claimed.
"""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
from threading import Thread, Lock
import time

import requests
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from connect_wireguard import TLSName
from sova.adapters.otel_collector import TelemetryProducer, collector_configuration
from sova.adapters.otlp_projection import project_otlp
from sova.domain.contracts import ResourceScope

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration"))
from test_otel_collector_runtime import certificates  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "collector", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    if (
        hashlib.sha256(a.collector.read_bytes()).hexdigest()
        != "d12ff0efa4fde0c0900152d92131efa7cddef94d265034360b030260ca2a30d7"
    ):
        raise ValueError("Exact inspected Darwin upstream Collector required")
    identity = json.loads((a.inputs / "identity.json").read_text())
    links = json.loads(a.connections.read_text())
    now = datetime.now(UTC)
    end = min(
        now + timedelta(minutes=5), datetime.fromisoformat(identity["expires_at"])
    )
    if identity["run_id"] != "b0be270e62" or end - now < timedelta(minutes=2):
        raise ValueError("Enough time in the owned trial required")
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    certificates(a.artifacts / "tls")
    tls = a.artifacts / "tls"
    secret = a.artifacts / "secrets"
    secret.mkdir(mode=0o700)
    tokens = {
        name: secrets.token_hex(32)
        for name in ("source", "target", "backend", "operator")
    }
    for name, value in tokens.items():
        (secret / (name + ".token")).write_text(value)
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    producers = tuple(
        TelemetryProducer(
            side,
            ResourceScope(
                identity["tenant_id"],
                identity["application_id"],
                "personal-controller",
                "health-gateway-" + side,
            ),
            code,
            free_port(),
            "/secrets/" + side + ".token",
            ("/healthz",),
            end,
        )
        for side in ("source", "target")
    )
    spans = []
    receipts = []
    errors = []
    lock = Lock()
    servers = []
    providers = []
    collector = None
    report = {
        "started_at": now.isoformat(),
        "measurement_scope": "Actual controller gateway HTTP requests fetching registered AWS/VM health; not app-server processing",
        "collector_sha256": hashlib.sha256(a.collector.read_bytes()).hexdigest(),
        "gateway_source_sha256": code,
        "migration_complete": False,
        "application_server_instrumented": False,
        "operational_monitoring_qualified": False,
        "slo_qualified": False,
        "passed": False,
    }

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            cert = self.connection.getpeercert()
            cn = dict(x for group in cert.get("subject", ()) for x in group).get(
                "commonName"
            )
            size = int(self.headers.get("Content-Length", "0"))
            if (
                self.path != "/v1/traces"
                or cn != "collector"
                or self.headers.get("Authorization") != "Bearer " + tokens["backend"]
                or not 0 < size <= 1048576
            ):
                self.send_response(403)
                self.end_headers()
                return
            try:
                found = project_otlp(
                    self.rfile.read(size),
                    producers,
                    authenticate_collector=lambda: None,
                    now=datetime.now(UTC),
                )
                with lock:
                    spans.extend(found)
            except Exception as e:
                with lock:
                    errors.append(type(e).__name__)
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

    def serve(handler, certname):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls / (certname + ".pem"), tls / (certname + ".key"))
        ctx.load_verify_locations(tls / "ca.pem")
        ctx.verify_mode = ssl.CERT_REQUIRED
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    try:
        backend = serve(Sink, "backend")
        config = collector_configuration(
            producers,
            backend_endpoint=f"https://127.0.0.1:{backend.server_port}",
            now=now,
        )
        for receiver, producer in zip(config["receivers"].values(), producers):
            receiver["protocols"]["http"]["endpoint"] = "127.0.0.1:" + str(
                producer.port
            )
        config["service"]["telemetry"] = {
            "metrics": {"level": "none"},
            "logs": {"level": "error"},
        }
        raw = (
            json.dumps(config)
            .replace("/tls/", str(tls) + "/")
            .replace("/secrets/", str(secret) + "/")
        )
        file = a.artifacts / "collector.json"
        file.write_text(raw)
        report["collector_configuration_sha256"] = hashlib.sha256(
            raw.encode()
        ).hexdigest()
        with (a.artifacts / "collector.log").open("x") as log:
            collector = subprocess.Popen(
                [str(a.collector), "--config=" + str(file)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 15
        for producer in producers:
            while True:
                if collector.poll() is not None:
                    raise RuntimeError(
                        "Inspected Collector stopped; inspect private log"
                    )
                try:
                    with socket.create_connection(
                        ("127.0.0.1", producer.port), timeout=0.2
                    ):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Collector startup budget exceeded")
                    time.sleep(0.05)
        for producer, side in zip(producers, ("source", "target")):
            provider = TracerProvider(resource=Resource.create(producer.labels))
            providers.append(provider)
            exporter = OTLPSpanExporter(
                endpoint=f"https://127.0.0.1:{producer.port}/v1/traces",
                certificate_file=str(tls / "ca.pem"),
                client_key_file=str(tls / "producer.key"),
                client_certificate_file=str(tls / "producer.pem"),
                headers={"Authorization": "Bearer " + tokens[side]},
                timeout=3,
            )
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            tracer = provider.get_tracer("sova.actual-health-gateway")

            def handler_for(side, tracer):
                class Gateway(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_GET(self):
                        if (
                            self.path != "/healthz"
                            or self.headers.get("Authorization")
                            != "Bearer " + tokens["operator"]
                        ):
                            self.send_response(403)
                            self.end_headers()
                            return
                        with tracer.start_as_current_span(
                            "GET /healthz", kind=trace.SpanKind.SERVER
                        ) as span:
                            started = time.monotonic()
                            status = 502
                            verified = False
                            try:
                                with requests.Session() as http:
                                    http.trust_env = False
                                    http.mount(
                                        "https://",
                                        TLSName(
                                            identity["environments"][side][
                                                "application_host"
                                            ]
                                        ),
                                    )
                                    response = http.get(
                                        f"https://127.0.0.1:{links['ports'][side + '_app']}/healthz",
                                        verify=str(a.inputs / "pki/ca.pem"),
                                        timeout=5,
                                    )
                                    verified = (
                                        response.status_code == 200
                                        and response.json()
                                        == {
                                            "application_id": identity[
                                                "application_id"
                                            ],
                                            "side": side,
                                        }
                                        and response.headers.get("X-Sova-Deployment")
                                        == identity["app_manifest"]
                                    )
                                    status = 200 if verified else 502
                            except requests.RequestException:
                                pass
                            span.set_attribute("http.route", "/healthz")
                            span.set_attribute("http.request.method", "GET")
                            span.set_attribute("http.response.status_code", status)
                            with lock:
                                receipts.append(
                                    {
                                        "side": side,
                                        "verified_application_https": verified,
                                        "status": status,
                                        "duration_ms": round(
                                            (time.monotonic() - started) * 1000, 3
                                        ),
                                        "deployment_digest": identity["app_manifest"],
                                    }
                                )
                            body = json.dumps(
                                {"verified_application_health": verified, "side": side}
                            ).encode()
                            self.send_response(status)
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)

                return Gateway

            gateway = serve(handler_for(side, tracer), "producer")
            with requests.Session() as client:
                client.trust_env = False
                for _ in range(3):
                    response = client.get(
                        f"https://127.0.0.1:{gateway.server_port}/healthz",
                        headers={"Authorization": "Bearer " + tokens["operator"]},
                        verify=str(tls / "ca.pem"),
                        cert=(str(tls / "producer.pem"), str(tls / "producer.key")),
                        timeout=10,
                    )
                    response.raise_for_status()
        for provider in providers:
            provider.force_flush(timeout_millis=3000)
        until = time.monotonic() + 10
        while len(spans) < 6 and time.monotonic() < until:
            time.sleep(0.05)
        report.update(
            passed=len(spans) == 6
            and len(receipts) == 6
            and all(x["verified_application_https"] for x in receipts)
            and not errors,
            actual_http_receipts=receipts,
            projected_gateway_spans=[
                {**asdict(x), "duration_ms": x.duration_ms} for x in spans
            ],
            projection_errors=errors,
        )
    finally:
        for server, thread in reversed(servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        for provider in providers:
            provider.shutdown()
        if collector is not None and collector.poll() is None:
            collector.terminate()
            collector.wait(timeout=8)
        report["collector_stopped"] = collector is None or collector.poll() is not None
        report["completed_at"] = datetime.now(UTC).isoformat()
        (a.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "actual_http_requests": len(receipts),
                "real_projected_gateway_spans": len(spans),
                "app_server_instrumented": False,
                "slo_qualified": False,
            }
        )
    )
    if not report["passed"]:
        raise RuntimeError(
            "Actual health telemetry did not qualify; private evidence retained"
        )


if __name__ == "__main__":
    main()
