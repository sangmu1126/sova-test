"""Run-owned worker process fixture; not the deployed Kubernetes CLI/catalog.

Real HandoffWorker.run, leases, effects and PostgreSQL probes are used. Only
the shared route and the after-apply pause are fixtures. There is deliberately
no quiescence provider: a dead/expired owner cannot be recovered on a guess.
"""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from postgres_business import PROBE_INSERT, SCHEMA, TABLES
import psycopg

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.service.main import _connect, _logging, _settings, schema_ready
from sova.service.config import ServiceConfig
from sova.service.postgres_execution import postgres_factory
from sova.service.worker import AdapterCatalog, Execution, HandoffWorker


class FixtureRoute:
    """Atomic local file shared by the toy HTTP app and owned worker processes.

    This actually chooses the app's next SQL connection, but is not a
    Kubernetes Service, an Edge gateway or a production route store.
    """
    def __init__(self, path, *, scope, databases):
        self.path, self.scope, self.databases = Path(path), scope, tuple(databases)

    def get(self):
        data = json.loads(self.path.read_text())
        if set(data) != {"scope", "database"} or data["scope"] != self.scope or data["database"] not in self.databases:
            raise ValueError("Fixture route scope or database mismatch")
        return data["database"]

    def set(self, database):
        if database not in self.databases:
            raise ValueError("Unregistered fixture route database")
        # The file is a fixture, not a CAS authority. The existing worker lease
        # is the authority; only that worker calls this after admission.
        fd, name = tempfile.mkstemp(prefix="route-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump({"scope": self.scope, "database": database}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def trace(path, **event):
    data = (json.dumps(event, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


class PauseAfterApply:
    def __init__(self, inner, spec):
        self.inner, self.spec, self.adapter_id = inner, spec, inner.adapter_id

    def validate(self, command):
        self.inner.validate(command)

    def apply(self, command):
        s = self.spec
        trace(s["trace"], event="apply_started", owner=s["owner"], action=command.action,
              command_digest=command.fingerprint)
        self.inner.apply(command)
        trace(s["trace"], event="apply_committed", owner=s["owner"], action=command.action,
              command_digest=command.fingerprint)
        if command.action == s["pause_after"]:
            trace(s["ready"], event="after_apply_barrier", command_digest=command.fingerprint)
            deadline = time.monotonic() + 30
            while not Path(s["release"]).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Owned after-apply barrier timed out")
                time.sleep(0.01)

    def readback(self, command, now):
        return self.inner.readback(command, now)


def child(path):
    spec = json.loads(Path(path).read_text())
    config = ServiceConfig.from_env()
    names = spec["names"]
    dsn = Path(spec["business_dsn_file"]).read_text()
    route = FixtureRoute(spec["route_file"], scope=spec["scope"], databases=(names["src"], names["tgt"]))

    def resolve(tenant, app):
        if (tenant, app) != (spec["tenant"], spec["application"]):
            raise ValueError("Wrong worker application")
        return {"connect": lambda db: psycopg.connect(dsn, dbname=db),
                "connect_as": lambda db, role: psycopg.connect(dsn, dbname=db, user=role),
                "source_db": names["src"], "target_db": names["tgt"],
                "scratch_db": names["scratch"], "other_db": names["other"],
                "app_role": spec["app_role"], "writer_roles": (spec["app_role"], spec["batch_role"]),
                "tables": TABLES, "schema_sql": SCHEMA, "probe_insert": PROBE_INSERT, "route": route}

    factory = postgres_factory(spec["adapter_id"], resolve)
    def wrapped(tenant, app, revision):
        execution = factory(tenant, app, revision)
        return Execution(PauseAfterApply(execution.adapter, spec), execution.measure)
    signer = Ed25519EvidenceSigner(
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(Path(spec["key_file"]).read_text())),
        issuer_id=config.evidence_issuer, key_id=spec["key_id"])
    class ObservedWorker(HandoffWorker):
        def drive(self, *args):
            record = super().drive(*args)
            trace(spec["trace"], event="drive_returned", owner=spec["owner"],
                  state=str(record.state) if record is not None else None)
            return record

    w = ObservedWorker(_connect(config), settings=_settings(config),
                      catalog=AdapterCatalog({spec["adapter_id"]: wrapped}), signer=signer,
                      clock=lambda: datetime.now(UTC), owner_id=spec["owner"],
                      lease_seconds=2, max_step_seconds=30, quiescence=None)
    def stop(*_):
        w.stopping.set()
        trace(spec["trace"], event="stop_requested", owner=spec["owner"])
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    trace(spec["trace"], event="loop_started", owner=spec["owner"])
    w.run((spec["tenant"],), poll_seconds=0.1, ready=lambda: schema_ready(w.connect))
    trace(spec["trace"], event="loop_stopped", owner=spec["owner"])


class WorkerProcess:
    def __init__(self, h, *, label, pause_after=None):
        # Parent-side controller only. The child must not import the HTTP test
        # fixtures or rely on pytest adding tests/integration to sys.path.
        from qualification_runtime import private_file

        self.h, self.label = h, label
        self.owner = "mq-worker-" + uuid4().hex
        self.path = h.directory / (label + ".json")
        self.ready = h.directory / (label + ".ready")
        self.release = h.directory / (label + ".release")
        self.trace_file = h.directory / "worker-trace.jsonl"
        key = private_file(h.directory / (label + ".key"), h.signer._key.private_bytes_raw().hex())
        dsn = private_file(h.directory / (label + ".dsn"), h.business["dsn"])
        private_file(self.path, json.dumps({
            "owner": self.owner, "tenant": h.tenant, "application": h.app,
            "names": h.business["names"], "business_dsn_file": str(dsn),
            "app_role": h.business["app"], "batch_role": h.business["batch"],
            "adapter_id": h.world["manifest"].adapter_id,
            "route_file": str(h.route.path), "scope": h.route.scope,
            "key_file": str(key), "key_id": h.signer.key_id,
            "pause_after": pause_after, "trace": str(self.trace_file),
            "ready": str(self.ready), "release": str(self.release)}))
        self.output_path = h.directory / (label + ".log")
        self.output = self.output_path.open("x")
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(self.path)],
                                         env=h.environment, stdout=self.output, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL)
        self.intentional_kill = False
        self.result = None

    def events(self):
        if not self.trace_file.exists():
            return []
        # Ignore only the unfinished tail of an actively appended trace. All
        # final artifacts are written after both processes have been waited.
        text = self.trace_file.read_text().rsplit("\n", 1)[0]
        return [json.loads(line) for line in text.splitlines()]

    def wait(self, check, *, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            if self.process.poll() is not None:
                raise RuntimeError("Worker exited while waiting; inspect its private log")
            time.sleep(0.02)
        raise RuntimeError("Owned worker did not reach the expected boundary")

    def kill_after_apply(self):
        if not self.ready.exists():
            raise RuntimeError("No after-apply boundary observed")
        self.intentional_kill = True
        self.process.kill()
        if self.process.wait(timeout=5) != -signal.SIGKILL:
            raise RuntimeError("Worker did not stop at the intended SIGKILL boundary")

    def stop(self):
        if self.result is not None:
            return self.result
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.wait(lambda: any(e["event"] == "stop_requested" and e["owner"] == self.owner
                                  for e in self.events()), timeout=5)
            self.release.touch(mode=0o600)
        try:
            code = self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise RuntimeError("Worker required an unplanned SIGKILL") from None
        finally:
            self.output.close()
        events = self.events()
        stopped = any(e["event"] == "loop_stopped" and e["owner"] == self.owner for e in events)
        artifacts = os.environ.get("SOVA_TEST_QUALIFICATION_ARTIFACTS")
        if artifacts:
            target = Path(artifacts) / f"worker-{self.h.case_name}-{self.label}.log"
            with target.open("xb") as file:
                file.write(self.output_path.read_bytes())
        if (self.intentional_kill and code != -signal.SIGKILL) or (not self.intentional_kill and (code != 0 or not stopped)):
            raise RuntimeError("Unexpected worker termination; inspect the preserved private log")
        self.result = {"owner": self.owner, "exit_code": code, "intentional_sigkill": self.intentional_kill,
                       "loop_stopped_recorded": stopped, "process_wait_completed": True}
        return self.result


if __name__ == "__main__":
    _logging()
    child(sys.argv[1])
