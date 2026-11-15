"""Workers run admitted handoffs on real PostgreSQL data: the adapter through
the effect ledger, every obligation measured by a probe and signed, the
authority advanced only by the coordinator. One worker holds a handoff at a
time; one that dies during a step is replaced only on proof it is gone."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from contract_world import build_world
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from handoff_drive import without_transport_control
from postgres_business import PROBE_INSERT, SCHEMA, TABLES, Route
import psycopg
import pytest
from test_service_control_plane import feeds_complete, register_everything, request_of

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.service_contracts import example_service_contract
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.handoff_authority import HandoffState
from sova.domain.service_contracts import ServiceContract
from sova.domain.verifier_registry import VerifierTrustChange
from sova.service.control_plane import ControlPlane, Principal, Role, Settings
from sova.service.postgres_execution import postgres_factory
from sova.service.quiescence import KubernetesWorkerQuiescence
from sova.service.worker import AdapterCatalog, Execution, HandoffWorker

pytestmark = pytest.mark.postgres
CHILD = Path(__file__).resolve().parents[1] / "worker_crash_child.py"


def admitted(postgres_database, now, business, *, witness_dir):
    """A handoff admitted through the service, its evidence producer trusted."""
    dsn, role = postgres_database
    tenant = "tenant-" + uuid4().hex
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = tenant
    without_transport_control(data)  # the local reference server has no TLS
    world = build_world(now, contract=ServiceContract.from_document(data))

    def connect():
        return psycopg.connect(dsn, user=role)

    clock = lambda: datetime.now(UTC)  # noqa: E731
    app = world["contract"].service_id
    approved = datetime.now(UTC) - timedelta(seconds=1)
    registry = PostgresVerifierRegistry(connect, tenant_id=tenant, application_id=app)
    registry.publish(VerifierTrustChange(tenant, app, 1, world["trust"], "security-requester",
                                         "security-approver", "Registry attestor", approved,
                                         approved + timedelta(minutes=1)))
    from sova.adapters.file_authority_witness import FileAuthorityWitness

    settings = Settings(registry_verifier_version="registry-attestor-1",
                        evidence_verifier="handoff-verifier-1",
                        evidence_issuer="handoff-producer",
                        witness_for=lambda t: FileAuthorityWitness(witness_dir / f"{t}.log",
                                                                   tenant_id=t))
    plane = ControlPlane(connect, settings=settings, clock=clock)
    people = {r: Principal(f"{r.value}-person", tenant, frozenset({r})) for r in Role}
    s = {"plane": plane, "world": world, "people": people, "tenant": tenant,
         "connect": connect, "app": app}
    register_everything(s)
    compiled = plane.compile(people[Role.PLANNER], request_of(world))
    assert compiled["status"] == "COMPILED", compiled["blockers"]
    plane.approve(people[Role.APPROVER], app, compiled["plan_id"], "approval-1")
    feeds_complete(s, compiled)
    plan = PostgresLifecycleRepository(connect, tenant_id=tenant).get_plan(
        app, compiled["plan_id"])
    key = Ed25519PrivateKey.generate()
    signer = Ed25519EvidenceSigner(key, issuer_id="handoff-producer", key_id="k1")
    scopes = tuple(sorted({o.scope for o in plan.obligations},
                          key=lambda x: (x.environment_id, x.resource_id)))
    approved = datetime.now(UTC) - timedelta(seconds=1)
    registry.publish(VerifierTrustChange(
        tenant, app, 1, VerifierTrust(signer.issuer_id, signer.key_id, signer.public_key_hex,
                                      scopes, ("handoff-verifier-1",),
                                      approved - timedelta(minutes=1),
                                      approved + timedelta(hours=1)),
        "security-requester", "security-approver", "Worker evidence producer", approved,
        approved + timedelta(minutes=1)))
    unit = plane.admit(people[Role.OPERATOR], app, "approval-1")["unit_digest"]
    names = business["names"]
    route = Route(names["src"])

    def resolve(tenant_id, application_id):
        return {"connect": business["connect"], "connect_as": business["connect_as"],
                "source_db": names["src"], "target_db": names["tgt"],
                "scratch_db": names["scratch"], "other_db": names["other"],
                "app_role": business["app"], "writer_roles": (business["app"],
                                                              business["batch"]),
                "tables": TABLES, "schema_sql": SCHEMA, "probe_insert": PROBE_INSERT,
                "route": route}

    adapter_id = world["manifest"].adapter_id
    catalog = AdapterCatalog({adapter_id: postgres_factory(adapter_id, resolve)})
    return {**s, "settings": settings, "catalog": catalog, "signer": signer, "key": key,
            "unit": unit, "plan": plan, "route": route, "clock": clock, "dsn": dsn,
            "role": role}


def worker(h, owner, **extra):
    return HandoffWorker(h["connect"], settings=h["settings"], catalog=h["catalog"],
                         signer=h["signer"], clock=h["clock"], owner_id=owner, **extra)


def test_a_worker_takes_an_admitted_handoff_to_completion(postgres_database, now, business,
                                                          tmp_path):
    h = admitted(postgres_database, now, business, witness_dir=tmp_path)
    w = worker(h, "worker-a")
    assert (h["app"], h["unit"]) in w.open_units(h["tenant"])
    record = w.drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == business["names"]["tgt"]
    assert (h["app"], h["unit"]) not in w.open_units(h["tenant"])  # done: nothing to take


def test_a_handoff_another_worker_holds_is_left_alone(postgres_database, now, business,
                                                      tmp_path):
    h = admitted(postgres_database, now, business, witness_dir=tmp_path)
    lifecycle = PostgresLifecycleRepository(h["connect"], tenant_id=h["tenant"])
    operation = "handoff-" + h["plan"].plan_id[-24:]
    PostgresExecutionOwnership(lifecycle).acquire(h["app"], operation, "worker-elsewhere",
                                                  lease_seconds=60)
    assert worker(h, "worker-b").drive(h["tenant"], h["app"], h["unit"]) is None


class SlowStep:
    """The adapter, with one step taking longer than a whole lease."""

    def __init__(self, inner, action, seconds):
        self.inner, self.action, self.seconds = inner, action, seconds
        self.adapter_id = inner.adapter_id

    def validate(self, command):
        self.inner.validate(command)

    def apply(self, command):
        self.inner.apply(command)
        if command.action == self.action:
            time.sleep(self.seconds)

    def readback(self, command, now):
        return self.inner.readback(command, now)


def test_a_step_longer_than_a_lease_keeps_its_lease_while_it_runs(postgres_database, now,
                                                                   business, tmp_path):
    """Copying a large table outlasts a lease; the keeper renews it during the
    step, so the readback is recorded under the same lease, not refused."""
    h = admitted(postgres_database, now, business, witness_dir=tmp_path)
    adapter_id = h["world"]["manifest"].adapter_id
    inner = h["catalog"].factories[adapter_id]

    def slow(tenant_id, application_id, revision):
        execution = inner(tenant_id, application_id, revision)
        return Execution(SlowStep(execution.adapter, "copy_content", 5), execution.measure)

    w = HandoffWorker(h["connect"], settings=h["settings"],
                      catalog=AdapterCatalog({adapter_id: slow}), signer=h["signer"],
                      clock=h["clock"], owner_id="worker-slow", lease_seconds=2,
                      max_step_seconds=60)
    record = w.drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)


def crashed_mid_step(h, business, tmp_path, owner):
    """Worker ``owner`` dies right after copying: the effect happened, its
    readback was never recorded. Its lease is then made to run out."""
    names = business["names"]
    spec = {"dsn": h["dsn"], "role": h["role"], "tenant": h["tenant"],
            "settings": {"registry_verifier_version": "registry-attestor-1",
                         "evidence_verifier": "handoff-verifier-1",
                         "evidence_issuer": "handoff-producer"},
            "witness": str(tmp_path / f"{h['tenant']}.log"),
            "key": h["key"].private_bytes_raw().hex(),
            "adapter_id": h["world"]["manifest"].adapter_id, "names": names,
            "app_role": business["app"], "batch_role": business["batch"],
            "local_dsn": business["servers"][names["src"]] if "servers" in business
            else os.environ["SOVA_TEST_POSTGRES_DSN"],
            "application": h["app"], "unit": h["unit"], "crash_after": "copy_content",
            "owner": owner}
    child = subprocess.run([sys.executable, str(CHILD), json.dumps(spec)],
                           capture_output=True, text=True, timeout=300,
                           env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")})
    assert child.returncode == 17, child.stderr[-2000:]  # died as planned, mid-step
    lifecycle = PostgresLifecycleRepository(h["connect"], tenant_id=h["tenant"])
    with lifecycle.transaction() as conn:  # make the lease run out now
        conn.execute("UPDATE sova_control.app_execution SET valid_until=clock_timestamp()"
                     " - interval '1 second' WHERE application_id=%s", (h["app"],))


def test_a_worker_lost_during_a_step_is_replaced_only_on_proof_it_is_gone(
        postgres_database, now, business, tmp_path):
    """Worker B finds A's step in flight; without proof that A's pod is gone it
    does nothing, with it it reconciles the step by readback (never re-runs
    it) and finishes the handoff."""
    h = admitted(postgres_database, now, business, witness_dir=tmp_path)
    pod_a, pod_b = str(uuid4()), str(uuid4())
    crashed_mid_step(h, business, tmp_path, f"worker-a-0.{pod_a}.{uuid4().hex[:12]}")
    still_running = KubernetesWorkerQuiescence(pod_running=lambda name, uid: True,
                                               clock=h["clock"])
    assert worker(h, "worker-b", quiescence=still_running).drive(
        h["tenant"], h["app"], h["unit"]) is None  # no proof: hands off
    owner_b = f"worker-b-0.{pod_b}.{uuid4().hex[:12]}"
    gone = KubernetesWorkerQuiescence(
        pod_running=lambda name, uid: False if (name, uid) == ("worker-a-0", pod_a)
        else None, clock=h["clock"], this_owner=owner_b)
    record = worker(h, owner_b, quiescence=gone).drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == business["names"]["tgt"]


def test_a_worker_restarted_in_its_own_pod_recovers_its_step(
        postgres_database, now, business, tmp_path):
    """The container restarts in place: same pod, so the API says it runs. The
    new process knows the holder was an earlier instance of its own pod (the
    kubelet starts a container only after the last one stopped) and recovers;
    any other worker still sees a running pod and keeps out."""
    h = admitted(postgres_database, now, business, witness_dir=tmp_path)
    pod = str(uuid4())
    crashed_mid_step(h, business, tmp_path, f"worker-a-0.{pod}.{uuid4().hex[:12]}")
    running = lambda name, uid: True  # noqa: E731 - the pod itself was never replaced
    other = f"worker-b-0.{uuid4()}.{uuid4().hex[:12]}"
    assert worker(h, other, quiescence=KubernetesWorkerQuiescence(
        pod_running=running, clock=h["clock"], this_owner=other)).drive(
        h["tenant"], h["app"], h["unit"]) is None
    again = f"worker-a-0.{pod}.{uuid4().hex[:12]}"
    record = worker(h, again, quiescence=KubernetesWorkerQuiescence(
        pod_running=running, clock=h["clock"], this_owner=again)).drive(
        h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == business["names"]["tgt"]
