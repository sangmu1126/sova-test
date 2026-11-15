"""A MySQL handoff through the whole control plane: registered, compiled,
approved and admitted through the service, then run by a worker - the MySQL
adapter through the effect ledger, every obligation measured by the MySQL
probes and signed, the authority advanced only by the coordinator."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from contract_world import build_world
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from handoff_drive import without_transport_control
from mysql_business import PROBE_INSERT, SCHEMA, TABLES
from mysql_world import Route
import psycopg
import pytest
from test_service_control_plane import feeds_complete, register_everything, request_of

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.service_contracts import example_service_contract
from sova.domain.handoff_authority import HandoffState
from sova.domain.service_contracts import ServiceContract
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import VerifierTrustChange
from sova.service.control_plane import ControlPlane, Principal, Role, Settings
from sova.service.mysql_execution import mysql_factory
from sova.service.worker import AdapterCatalog, HandoffWorker

pytestmark = [pytest.mark.postgres, pytest.mark.mysql]


@pytest.fixture
def mysql_handoff(postgres_database, now, mysql_business, tmp_path):
    dsn, role = postgres_database  # the control plane's own database
    tenant = "tenant-" + uuid4().hex
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = tenant
    without_transport_control(data)  # the local MySQL server has no TLS
    world = build_world(now, contract=ServiceContract.from_document(data), engine="mysql")

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
    b, names = mysql_business, mysql_business["names"]
    route = Route(names["src"])

    def resolve(tenant_id, application_id):
        return {"connect": b["connect"], "connect_as": b["connect_as"],
                "source_db": names["src"], "target_db": names["tgt"],
                "scratch_db": names["scratch"], "other_db": names["other"],
                "app_role": b["app"], "writer_roles": (b["app"], b["batch"]),
                "tables": TABLES, "schema_sql": SCHEMA, "probe_insert": PROBE_INSERT,
                "route": route,
                "zone_of": lambda database: "approved-zone-a"}  # the operator's inventory

    adapter_id = world["manifest"].adapter_id
    worker = HandoffWorker(connect, settings=settings,
                           catalog=AdapterCatalog({adapter_id: mysql_factory(adapter_id,
                                                                              resolve)}),
                           signer=signer, clock=clock, owner_id="worker-mysql")
    return {"worker": worker, "tenant": tenant, "app": app, "unit": unit, "route": route,
            "business": b, "plan": plan, "connect": connect, "adapter_id": adapter_id}


def test_a_mysql_unit_is_handed_off_by_the_control_plane(mysql_handoff):
    h, b = mysql_handoff, mysql_handoff["business"]
    names = b["names"]
    assert h["adapter_id"] == "mysql-offline-copy"
    record = h["worker"].drive(h["tenant"], h["app"], h["unit"])
    assert record.state == HandoffState.HANDOFF_COMPLETE, (record.reason, record.held_from)
    assert h["route"].get() == names["tgt"]
    # The application now writes on the target only.
    with b["connect_as"](names["tgt"], b["app"]) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO orders VALUES (5001, 'after', 'after-1', 5)")
        conn.commit()
    errors = b["pymysql"].err
    with pytest.raises((errors.OperationalError, errors.ProgrammingError)):
        b["connect_as"](names["src"], b["app"])
    # Every obligation was measured by a probe and signed, none failed.
    lifecycle = PostgresLifecycleRepository(h["connect"], tenant_id=h["tenant"])
    with lifecycle.transaction() as conn:
        documents = [r[0] for r in conn.execute(
            "SELECT document FROM sova_control.signed_security_evidence WHERE tenant_id=%s "
            "AND issuer_id='handoff-producer' ORDER BY stored_at", (h["tenant"],))]
    results = {}
    for document in documents:  # the latest result of each obligation
        results[document["evidence"]["obligation_id"]] = document["evidence"]["result"]
    assert results and set(results.values()) == {"pass"}, results
    measured = {o.obligation_id for o in h["plan"].obligations}
    assert measured <= set(results), sorted(measured - set(results))
