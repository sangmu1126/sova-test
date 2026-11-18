"""A handoff prepared through the whole control plane, for end-to-end tests:
registered, compiled, approved and admitted through the service, with the
worker's evidence key trusted for the plan's obligations. What runs the unit
(the catalog factory) is the test's; the probes' observations, which the
worker signs as digests only, are kept here so a test can read them."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from contract_world import build_world
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from handoff_drive import without_transport_control
import psycopg
from test_service_control_plane import feeds_complete, register_everything, request_of

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.service_contracts import example_service_contract
from sova.domain.service_contracts import ServiceContract
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import VerifierTrustChange
from sova.service.control_plane import ControlPlane, Principal, Role, Settings
from sova.service.worker import AdapterCatalog, Execution, HandoffWorker

BOARD = {"id": "BOARD-01", "description": "모든 게시글의 첨부 파일이 업로드 폴더에 있다."}


def board_contract():
    """The board's contract: its database and uploads are protected, every post's
    attachment must exist, and - a file-state application having no read-only
    mode - its owner accepts a write stop of minutes, not seconds."""
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = "tenant-" + uuid4().hex
    data["spec"]["data"]["protected_sets"] = ["board.db", "uploads"]
    data["spec"]["business"]["invariants"] = [BOARD]
    without_transport_control(data)  # agent Pods are reached without TLS (see the probes)
    data["spec"]["availability"]["max_write_admission_pause_ms"] = 180000
    data["spec"]["availability"]["max_client_completion_wait_ms"] = 240000
    return data


def prepared(postgres_database, now, tmp_path, data, *, engine, strategy="offline-copy"):
    dsn, role = postgres_database  # the control plane's own database
    tenant = data["metadata"]["tenant_id"]
    # The plan's evidence is dated when the fixtures are up, not when the test
    # began (a managed cluster takes minutes to set up a board and a game), and
    # holds for the whole handoff there (minutes more than on kind).
    now = max(now, datetime.now(UTC) - timedelta(seconds=1))
    world = build_world(now, contract=ServiceContract.from_document(data), engine=engine,
                        valid_for=timedelta(minutes=20),
                        strategy=strategy)

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
    return {"connect": connect, "settings": settings, "signer": signer, "clock": clock,
            "tenant": tenant, "app": app, "unit": unit, "plan": plan,
            "adapter_id": world["manifest"].adapter_id}


def worker_for(h, factory, *, owner):
    """A worker running ``factory``; the observations it signs are kept in
    ``h["observations"]``."""
    h["observations"] = observations = {}

    def keeping(*args):
        execution = factory(*args)

        def measure(obligation, times):
            passed, observation = execution.measure(obligation, times)
            observations[obligation["id"]] = observation
            return passed, observation
        return Execution(execution.adapter, measure)

    return HandoffWorker(h["connect"], settings=h["settings"],
                         catalog=AdapterCatalog({h["adapter_id"]: keeping}),
                         signer=h["signer"], clock=h["clock"], owner_id=owner)


def signed_results(h):
    """The latest signed result of each obligation."""
    lifecycle = PostgresLifecycleRepository(h["connect"], tenant_id=h["tenant"])
    with lifecycle.transaction() as conn:
        documents = [r[0] for r in conn.execute(
            "SELECT document FROM sova_control.signed_security_evidence WHERE tenant_id=%s "
            "AND issuer_id='handoff-producer' ORDER BY stored_at", (h["tenant"],))]
    return {d["evidence"]["obligation_id"]: d["evidence"]["result"] for d in documents}
