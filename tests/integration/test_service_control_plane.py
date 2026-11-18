"""The control plane as a service, end to end on real PostgreSQL: registered
documents, verified attestations, a compiled plan, an approval by someone else,
admission from the same request, and the authority it opens.

Each operation is called as a principal with roles; the rules are checked in
the service, so every front end (HTTP, CLI, worker) gets the same answer.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from contract_world import build_world
from handoff_support import COLLECTOR
import psycopg
import pytest
from witness_support import MemoryAuthorityWitness

from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.adapters.registration import registry_codec as codecs
from sova.application.service_contracts import example_service_contract
from sova.domain.change_impact import ImpactIndex
from sova.domain.contracts import ContractError
from sova.domain.service_contracts import ServiceContract
from sova.domain.verifier_registry import VerifierTrustChange
from sova.service.control_plane import ControlPlane, Forbidden, PlanRequest, Principal, Role, \
    Settings

pytestmark = pytest.mark.postgres


@pytest.fixture
def service(postgres_database, now):
    dsn, role = postgres_database
    tenant = "tenant-" + uuid4().hex
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = tenant
    world = build_world(now, contract=ServiceContract.from_document(data))

    def connect():
        return psycopg.connect(dsn, user=role)

    clock = lambda: datetime.now(UTC)  # noqa: E731
    witnesses = {}
    app = world["contract"].service_id
    approved = datetime.now(UTC) - timedelta(seconds=1)
    PostgresVerifierRegistry(connect, tenant_id=tenant, application_id=app).publish(
        VerifierTrustChange(tenant, app, 1, world["trust"], "security-requester",
                            "security-approver", "Register the registry attestor",
                            approved, approved + timedelta(minutes=1)))
    plane = ControlPlane(connect, clock=clock, settings=Settings(
        registry_verifier_version="registry-attestor-1",
        evidence_verifier="handoff-verifier-1", evidence_issuer="handoff-producer",
        # One witness per tenant for the life of the service, as a file or an
        # Object Lock bucket is; a fresh one per call would look like a rollback.
        witness_for=lambda tenant_id: witnesses.setdefault(tenant_id,
                                                           MemoryAuthorityWitness())))
    people = {r: Principal(f"{r.value}-person", tenant, frozenset({r})) for r in Role}
    return {"plane": plane, "world": world, "people": people, "tenant": tenant,
            "connect": connect, "app": app}


def register_everything(s):
    w, plane, registrar = s["world"], s["plane"], s["people"][Role.REGISTRAR]
    for value in (w["contract"], w["snapshot"], w["source"], w["target"], w["manifest"],
                  w["qualification"]):
        plane.register(registrar, codecs.kind_of(value), codecs.subject_of(value),
                       codecs.encode(value), expected_revision=0)
    for subject in (w["contract"], w["snapshot"], w["qualification"]):
        plane.attest(s["people"][Role.ATTESTOR], w["sign"](subject).document)


def request_of(w):
    return PlanRequest(w["contract"].service_id, w["snapshot"].snapshot_id,
                       w["source"].profile_id, w["target"].profile_id,
                       codecs.subject_of(w["manifest"]), w["qualification"].record_id,
                       ("api-a", "api-b", "database"), 2000)


def feeds_complete(s, compiled):
    lifecycle = PostgresLifecycleRepository(s["connect"], tenant_id=s["tenant"])
    plan = lifecycle.get_plan(compiled["application_id"], compiled["plan_id"])
    revision = PostgresCompiledPlans(s["connect"], tenant_id=s["tenant"]).load(
        compiled["compiled_digest"])
    feed = PostgresChangeFeed(s["connect"], tenant_id=s["tenant"])
    for environment in sorted(ImpactIndex.from_plan(revision, plan).required_feeds):
        feed.advance(environment, collector_id=COLLECTOR,
                     observed_through=datetime.now(UTC) - timedelta(seconds=1))


def test_a_handoff_goes_from_registration_to_admission_through_the_service(service):
    s, p = service, service["people"]
    register_everything(s)
    compiled = s["plane"].compile(p[Role.PLANNER], request_of(s["world"]))
    assert compiled["status"] == "COMPILED", compiled["blockers"]
    s["plane"].approve(p[Role.APPROVER], compiled["application_id"], compiled["plan_id"],
                       "approval-1")
    feeds_complete(s, compiled)
    admitted = s["plane"].admit(p[Role.OPERATOR], compiled["application_id"], "approval-1")
    status = s["plane"].status(p[Role.AUDITOR], admitted["application_id"],
                               admitted["unit_digest"])
    assert status["state"] == "SOURCE_ACTIVE" and status["next_operation"] == 0
    assert [h["actor"] for h in status["history"]] == [p[Role.OPERATOR].subject]


def test_each_operation_needs_its_role(service):
    s, p = service, service["people"]
    w = s["world"]
    with pytest.raises(Forbidden):
        s["plane"].register(p[Role.PLANNER], "environment_profile", w["source"].profile_id,
                            codecs.encode(w["source"]), expected_revision=0)
    with pytest.raises(Forbidden):
        s["plane"].attest(p[Role.REGISTRAR], w["sign"](w["contract"]).document)
    with pytest.raises(Forbidden):
        s["plane"].compile(p[Role.APPROVER], request_of(w))


def test_whoever_compiled_a_plan_cannot_approve_it(service):
    s = service
    register_everything(s)
    both = Principal("planner-and-approver", s["tenant"],
                     frozenset({Role.PLANNER, Role.APPROVER}))
    compiled = s["plane"].compile(both, request_of(s["world"]))
    with pytest.raises(Forbidden, match="Separation of duties"):
        s["plane"].approve(both, compiled["application_id"], compiled["plan_id"], "approval-1")


def test_an_attestation_for_another_tenant_or_without_trust_is_refused(service):
    s, p = service, service["people"]
    stranger = Principal("attestor-person", "tenant-" + uuid4().hex,
                         frozenset({Role.ATTESTOR}))
    with pytest.raises(Forbidden, match="another tenant"):
        s["plane"].attest(stranger, s["world"]["sign"](s["world"]["contract"]).document)
    other = build_world(s["world"]["now"], contract=s["world"]["contract"])  # untrusted key
    with pytest.raises(ContractError):
        s["plane"].attest(p[Role.ATTESTOR], other["sign"](other["contract"]).document)


def test_inputs_changed_after_approval_refuse_admission(service):
    s, p = service, service["people"]
    register_everything(s)
    w = s["world"]
    compiled = s["plane"].compile(p[Role.PLANNER], request_of(w))
    s["plane"].approve(p[Role.APPROVER], compiled["application_id"], compiled["plan_id"],
                       "approval-1")
    feeds_complete(s, compiled)
    # The target environment is re-registered after the approval.
    moved = replace(w["target"], revision=w["target"].revision + 1)
    s["plane"].register(p[Role.REGISTRAR], "environment_profile", moved.profile_id,
                        codecs.encode(moved), expected_revision=1)
    with pytest.raises(ContractError):
        s["plane"].admit(p[Role.OPERATOR], compiled["application_id"], "approval-1")


def test_a_plan_with_blockers_is_not_stored(service):
    s, p = service, service["people"]
    register_everything(s)
    request = replace(request_of(s["world"]), nodes=("api-a",))  # splits a shared write
    result = s["plane"].compile(p[Role.PLANNER], request)
    assert result["status"] != "COMPILED" and result["blockers"]
    assert "plan_id" not in result
