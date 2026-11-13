"""Persisted authority transitions driven by real signed evidence in PostgreSQL.

Synthetic contracts, local Ed25519 keys, and simulated effect outcomes; no
live environment is changed and no adapter command is executed.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from threading import Barrier
from uuid import uuid4

from contract_world import build_world
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import psycopg
import pytest

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner, Ed25519EvidenceVerifier
from sova.adapters.postgres_handoff_authority import PostgresHandoffAuthority
from sova.adapters.postgres_handoff_claims import PostgresHandoffClaims
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_signed_evidence import PostgresSignedEvidence
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.handoff_admission import HandoffAdmission
from sova.application.handoff_coordinator import HandoffCoordinator, SignedObligationEvidence
from sova.application.service_contracts import example_service_contract
from sova.application.signed_evidence import SignedEvidenceAuthority
from sova.domain.contracts import ContractError
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan
from sova.domain.plans import PlanApproval, PlanPurpose
from sova.domain.service_contracts import ServiceContract
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import VerifierTrustChange
from sova.ports.plans import RepositoryConflict

pytestmark = pytest.mark.postgres
VERIFIER = "handoff-verifier-1"


@pytest.fixture
def handoff(postgres_database, now):
    dsn, role = postgres_database
    tenant = "tenant-" + uuid4().hex

    def connect():
        return psycopg.connect(dsn, user=role)

    data = example_service_contract().document
    data["metadata"]["tenant_id"] = tenant
    world = build_world(now, contract=ServiceContract.from_document(data))
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)

    def recompile():
        return compile_revision(
            world["contract"], world["snapshot"],
            requested_nodes=("api-a", "api-b", "database"), expected_tenant=tenant,
            source=world["source"], target=world["target"], manifest=world["manifest"],
            qualification=world["qualification"], rows=2000, authenticate=world["auth"],
            clock=lambda: datetime.now(UTC),
        )

    revision, result = recompile()
    assert result["status"] == "COMPILED"
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    verifiers = {k: VERIFIER for k in kinds}
    plan = lower_plan(revision, created_by="planner", verifiers=verifiers)
    app = plan.application_id
    lifecycle = PostgresLifecycleRepository(connect, tenant_id=tenant,
                                            clock=lambda: datetime.now(UTC))
    lifecycle.save_plan(plan)
    lifecycle.save_approval(app, plan.plan_id, "approval-1", PlanApproval(
        plan.fingerprint, "security-approver", PlanPurpose.MIGRATION_PHASE,
        now + timedelta(minutes=3)))
    claims = PostgresHandoffClaims(connect, tenant_id=tenant)
    HandoffAdmission(lifecycle=lifecycle, claims=claims,
                     clock=lambda: datetime.now(UTC)).admit(
        app, "approval-1", recompile=recompile, verifiers=verifiers)

    # One registered evidence producer for every obligation scope of this plan.
    signer = Ed25519EvidenceSigner(Ed25519PrivateKey.generate(),
                                   issuer_id="handoff-producer", key_id="k1")
    scopes = tuple(sorted({o.scope for o in plan.obligations},
                          key=lambda s: (s.environment_id, s.resource_id)))
    registry = PostgresVerifierRegistry(connect, tenant_id=tenant, application_id=app)
    registry.publish(VerifierTrustChange(
        tenant, app, 1,
        VerifierTrust(signer.issuer_id, signer.key_id, signer.public_key_hex, scopes,
                      (VERIFIER,), now - timedelta(minutes=1), now + timedelta(hours=1)),
        "security-requester", "security-approver", "Register handoff evidence producer",
        now, now + timedelta(minutes=1)))
    signed = SignedEvidenceAuthority(
        PostgresSignedEvidence(connect, tenant_id=tenant),
        Ed25519EvidenceVerifier(registry.resolve), clock=lambda: datetime.now(UTC))
    store = PostgresHandoffAuthority(connect, tenant_id=tenant)
    coordinator = HandoffCoordinator(
        store=store,
        evidence=SignedObligationEvidence(
            current_envelope=PostgresSignedEvidence(connect, tenant_id=tenant).current_envelope,
            resolve_trust=registry.resolve, issuers={VERIFIER: signer.issuer_id},
            clock=lambda: datetime.now(UTC)),
        clock=lambda: datetime.now(UTC))
    counter = iter(range(10_000))

    def prove(obligation_id, result=VerificationResult.PASS, *, revisions=None):
        o = next(x for x in plan.obligations if x.obligation_id == obligation_id)
        observed = datetime.now(UTC) - timedelta(seconds=1) + timedelta(
            microseconds=next(counter))
        record = EvidenceRecord(
            f"ev-{uuid4().hex[:12]}", o.obligation_id, o.scope, revisions or plan.revisions,
            VERIFIER, observed, observed + timedelta(minutes=10), result,
            "observation", "a" * 64)
        signed.ingest(signer.sign(record, issued_at=observed))

    ops = json.loads(revision.canonical)["operations"]
    return dict(revision=revision, plan=plan, store=store, coordinator=coordinator,
                claims=claims, prove=prove, ops=ops, app=app)


def step(h, op, outcome=EffectOutcome.EFFECT_CONFIRMED):
    return h["coordinator"].apply_effect(h["revision"], h["plan"],
                                         operation_id=op["operation_id"],
                                         outcome=outcome, actor_id="coordinator")


def test_signed_evidence_drives_the_handoff_to_completion(handoff):
    h = handoff
    record = h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    assert record.state == HandoffState.SOURCE_ACTIVE
    for op in h["ops"]:
        if op["required_evidence"]:
            with pytest.raises(ContractError, match="evidence"):
                step(h, op)
        for obligation_id in op["required_evidence"]:
            h["prove"](obligation_id)
        record = step(h, op)
    assert record.state == HandoffState.HANDOFF_COMPLETE
    assert record.owner_environment == record.target_environment
    history = h["store"].history(h["app"], record.unit_digest)
    assert [rev for rev, *_ in history] == list(range(1, len(h["ops"]) + 2))


def test_newer_failed_or_foreign_evidence_does_not_satisfy_a_step(handoff):
    h = handoff
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    first = h["ops"][0]
    target = first["required_evidence"][0]
    for obligation_id in first["required_evidence"]:
        h["prove"](obligation_id)
    h["prove"](target, VerificationResult.FAIL)  # newer FAIL invalidates the PASS
    with pytest.raises(ContractError, match="evidence"):
        step(h, first)
    other = replace(h["plan"].revisions, policy="9" * 64)
    h["prove"](target, revisions=other)  # bound to another plan's inputs
    with pytest.raises(ContractError, match="evidence"):
        step(h, first)
    h["prove"](target)
    assert step(h, first).state == HandoffState.TARGET_PREPARED


def test_unknown_effect_holds_and_persists_until_reconciled(handoff):
    h = handoff
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    first, second = h["ops"][:2]
    for obligation_id in first["required_evidence"]:
        h["prove"](obligation_id)
    held = step(h, first, EffectOutcome.EFFECT_UNKNOWN)
    assert held.state == HandoffState.SAFE_HOLD
    assert h["store"].current(h["app"], held.unit_digest) == held
    with pytest.raises(ContractError, match="SAFE_HOLD"):
        step(h, second)
    resumed = h["coordinator"].reconcile(h["revision"], h["plan"],
                                         outcome=EffectOutcome.EFFECT_CONFIRMED,
                                         actor_id="coordinator")
    assert resumed.state == HandoffState.TARGET_PREPARED


def test_concurrent_coordinators_cannot_both_advance(handoff):
    h = handoff
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    first = h["ops"][0]
    for obligation_id in first["required_evidence"]:
        h["prove"](obligation_id)
    start = Barrier(2)

    def race(_):
        start.wait()
        try:
            return str(step(h, first).state)
        except (RepositoryConflict, ContractError):
            return "REFUSED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(race, range(2)))
    assert outcomes == ["REFUSED", "TARGET_PREPARED"]
    record = h["store"].current(h["app"], json.loads(h["revision"].canonical)["unit"]["unit_digest"])
    assert record.revision == 2


def test_released_claims_and_unfinished_units_refuse_changes(handoff):
    h = handoff
    record = h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    with pytest.raises(RepositoryConflict, match="unfinished"):
        h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    h["claims"].release(application_id=h["app"], plan_digest=h["plan"].fingerprint,
                        actor_id="coordinator")
    with pytest.raises(RepositoryConflict, match="not admitted"):
        h["coordinator"].abort(h["revision"], h["plan"], target_inactive_verified=True,
                               actor_id="coordinator")
    assert h["store"].current(h["app"], record.unit_digest) == record
