"""A file-state application's volume handed off by the whole control plane:
registered, compiled, approved and admitted through the service, then run by
a worker - the volume adapter through the effect ledger, every obligation
measured by the volume probes and signed, the authority advanced only by the
coordinator. The application is the SQLite-and-uploads board of
``volume_business``; another tenant's volume lives in a namespace of its own."""

from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from contract_world import build_world
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from handoff_drive import without_transport_control
import psycopg
import pytest
from test_service_control_plane import feeds_complete, register_everything, request_of
from volume_business import PYTHON, UID, bare_pod
from volume_world import Route, VolumeWorld

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.domain.service_contracts import ServiceContract
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import VerifierTrustChange
from sova.service.control_plane import ControlPlane, Principal, Role, Settings
from sova.service.volume_execution import volume_factory
from sova.service.worker import AdapterCatalog, Execution, HandoffWorker

pytestmark = [pytest.mark.postgres, pytest.mark.kubernetes]

BOARD = {"id": "BOARD-01", "description": "모든 게시글의 첨부 파일이 업로드 폴더에 있다."}


@pytest.fixture
def other_tenant():
    """Another tenant's namespace with a bound claim, read with its own token."""
    from kind_support import delete_namespace, kind_namespace, kubectl

    claim = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "records"},
             "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "standard",
                      "resources": {"requests": {"storage": "1Gi"}}}}
    holder = bare_pod("holder", "records", ["python3", "-c", "import time; time.sleep(86400)"],
                      read_only=True)
    context, namespace, controls = kind_namespace(
        [{"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["get"]}],
        [claim, holder], prefix="other", controls_class=KubernetesVolumeControls)
    try:
        kubectl(context, "-n", namespace, "wait", "--for=condition=Ready", "pod/holder",
                "--timeout=120s")
        yield controls()
    finally:
        delete_namespace(context, namespace)


@pytest.fixture
def volume_handoff(postgres_database, now, volume_business, other_tenant, tmp_path):
    dsn, role = postgres_database  # the control plane's own database
    tenant = "tenant-" + uuid4().hex
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = tenant
    data["spec"]["data"]["protected_sets"] = ["board.db", "uploads"]
    data["spec"]["business"]["invariants"] = [BOARD]
    without_transport_control(data)  # agent Pods are reached without TLS (see the probes)
    # A file-state application has no read-only mode: its write stop is downtime,
    # and the contract says how much of it the owner accepts.
    data["spec"]["availability"]["max_write_admission_pause_ms"] = 180000
    data["spec"]["availability"]["max_client_completion_wait_ms"] = 240000
    world = build_world(now, contract=ServiceContract.from_document(data),
                        engine="kubernetes-volume")

    def connect():
        return psycopg.connect(dsn, user=role)

    clock = lambda: datetime.now(UTC)  # noqa: E731
    app = world["contract"].service_id
    registry = PostgresVerifierRegistry(connect, tenant_id=tenant, application_id=app)
    approved = datetime.now(UTC) - timedelta(seconds=1)
    registry.publish(VerifierTrustChange(tenant, app, 1, world["trust"], "security-requester",
                                         "security-approver", "Registry attestor", approved,
                                         approved + timedelta(minutes=1)))
    settings = Settings(registry_verifier_version="registry-attestor-1",
                        evidence_verifier="handoff-verifier-1",
                        evidence_issuer="handoff-producer",
                        witness_for=lambda t: FileAuthorityWitness(tmp_path / f"{t}.log",
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
    signer = Ed25519EvidenceSigner(Ed25519PrivateKey.generate(), issuer_id="handoff-producer",
                                   key_id="k1")
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
    board = VolumeWorld(volume_business)
    controls = volume_business["controls"]()
    route = Route(controls, "board", {"board_src": {"app": "board", "side": "source"},
                                      "board_tgt": {"app": "board", "side": "target"}},
                  converge_seconds=60)

    def resolve(tenant_id, application_id):
        return {"source": board._side("source", controls),
                "target": board._side("target", controls), "route": route,
                "routes": ("board_src", "board_tgt"), "agent_key": VolumeWorld.KEY,
                "agent_image": PYTHON, "run_as": (UID, UID), "storage_class": "standard",
                "write_probe": (8080, "/probe"), "databases": ("board.db",),
                "invariants": {"BOARD-01": {
                    "database": "board.db", "under": "uploads",
                    "files": "SELECT attachment FROM posts WHERE attachment IS NOT NULL"}},
                "zone_of": lambda side: "approved-zone-a",  # the operator's inventory
                "other": (other_tenant, "records"), "scratch_claim": "sova-restore"}

    adapter_id = world["manifest"].adapter_id
    observations, base = {}, volume_factory(adapter_id, resolve)

    def factory(*args):  # the probes' observations are signed as digests; keep them here
        execution = base(*args)

        def measure(obligation, times):
            passed, observation = execution.measure(obligation, times)
            observations[obligation["id"]] = observation
            return passed, observation
        return Execution(execution.adapter, measure)

    worker = HandoffWorker(connect, settings=settings,
                           catalog=AdapterCatalog({adapter_id: factory}),
                           signer=signer, clock=clock, owner_id="worker-volume")
    return {"worker": worker, "tenant": tenant, "app": app, "unit": unit, "route": route,
            "board": board, "plan": plan, "connect": connect, "adapter_id": adapter_id,
            "observations": observations}


def test_a_volume_unit_is_handed_off_by_the_control_plane(volume_handoff):
    h, board = volume_handoff, volume_handoff["board"]
    assert h["adapter_id"] == "volume-offline-copy"
    board.business_write(2)
    before = board.posts("source")
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == "board_tgt"
    assert board.posts("target") == before  # rows, integrity and every upload
    assert not board.can_write("source", "app") and not board.can_write("source", "other")
    assert board.can_write("target", "app") and not board.can_write("target", "other")
    # Every obligation was measured by a probe and signed, none failed.
    lifecycle = PostgresLifecycleRepository(h["connect"], tenant_id=h["tenant"])
    with lifecycle.transaction() as conn:
        documents = [r[0] for r in conn.execute(
            "SELECT document FROM sova_control.signed_security_evidence WHERE tenant_id=%s "
            "AND issuer_id='handoff-producer' ORDER BY stored_at", (h["tenant"],))]
    results = {}
    for document in documents:  # the latest result of each obligation
        results[document["evidence"]["obligation_id"]] = document["evidence"]["result"]
    assert results and set(results.values()) == {"pass"}, {
        k: v for k, v in results.items() if v != "pass"}
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
    # Nothing of the probes or the adapter is left behind.
    pods = board._pods()
    assert not [p["metadata"]["name"] for p in pods
                if p["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == "sova"]
    claims = json.loads(board.run("get", "pvc", "-o", "json"))["items"]
    assert not [c for c in claims if c["metadata"]["name"].startswith("sova-restore")
                and not c["metadata"].get("deletionTimestamp")]
    seen = h["observations"]
    assert seen["data.board.db"]["method"] == "sqlite_logical"
    assert seen["business.BOARD-01"]["missing"] == [] and seen["business.BOARD-01"]["referenced"]
    assert seen["fence.source_writes_denied"]["unchanged"] is True
    print("measured:", json.dumps({k: seen[k] for k in sorted(seen)
                                   if k.startswith(("availability.", "operations."))}))
