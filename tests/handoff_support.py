"""Shared real-PostgreSQL handoff setup: synthetic inputs, local Ed25519 keys."""

from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from contract_world import build_world
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import psycopg
from witness_support import MemoryAuthorityWitness

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner, Ed25519EvidenceVerifier
from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.postgres_handoff_authority import PostgresHandoffAuthority
from sova.adapters.postgres_handoff_claims import PostgresHandoffClaims
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_signed_evidence import PostgresSignedEvidence
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.change_monitor import ChangeImpactSource
from sova.application.exit_readiness import ExitReadiness
from sova.application.handoff_admission import HandoffAdmission
from sova.application.handoff_coordinator import HandoffCoordinator, SignedObligationEvidence
from sova.application.service_contracts import example_service_contract
from sova.application.signed_evidence import SignedEvidenceAuthority
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.change_impact import ImpactIndex
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan
from sova.domain.plans import PlanApproval, PlanPurpose
from sova.domain.service_contracts import ServiceContract
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import VerifierTrustChange

VERIFIER = "handoff-verifier-1"
COLLECTOR = "change-collector-1"


def complete_feeds(feed, revision, plan):
    """Report every change feed the plan depends on as complete up to now."""
    for environment in sorted(ImpactIndex.from_plan(revision, plan).required_feeds):
        feed.advance(environment, collector_id=COLLECTOR,
                     observed_through=datetime.now(UTC) - timedelta(milliseconds=5))


def admitted_handoff(postgres_database, now, *, witness_for=None):
    """A compiled, lowered, stored, approved and admitted synthetic handoff."""
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
    feed = PostgresChangeFeed(connect, tenant_id=tenant)
    changes = ChangeImpactSource(feed=feed, clock=lambda: datetime.now(UTC))
    complete_feeds(feed, revision, plan)
    compiled = PostgresCompiledPlans(connect, tenant_id=tenant)
    HandoffAdmission(lifecycle=lifecycle, claims=claims, changes=changes, compiled=compiled,
                     clock=lambda: datetime.now(UTC)).admit(
        app, "approval-1", recompile=recompile, verifiers=verifiers)

    # One registered evidence producer for every obligation scope of this plan.
    signer = Ed25519EvidenceSigner(Ed25519PrivateKey.generate(),
                                   issuer_id="handoff-producer", key_id="k1")
    scopes = tuple(sorted({o.scope for o in plan.obligations},
                          key=lambda s: (s.environment_id, s.resource_id)))
    registry = PostgresVerifierRegistry(connect, tenant_id=tenant, application_id=app)
    trusted = {"revision": 0, "scopes": set()}

    def trust_scopes(more):
        """Publish the producer's trust over its current scopes plus ``more``."""
        trusted["revision"] += 1
        trusted["scopes"] |= set(more)
        registry.publish(VerifierTrustChange(
            tenant, app, trusted["revision"],
            VerifierTrust(signer.issuer_id, signer.key_id, signer.public_key_hex,
                          tuple(sorted(trusted["scopes"],
                                       key=lambda s: (s.environment_id, s.resource_id))),
                          (VERIFIER,), now - timedelta(minutes=1), now + timedelta(hours=1)),
            "security-requester", "security-approver", "Register handoff evidence producer",
            now, now + timedelta(minutes=1)))

    trust_scopes(scopes)
    signed = SignedEvidenceAuthority(
        PostgresSignedEvidence(connect, tenant_id=tenant),
        Ed25519EvidenceVerifier(registry.resolve), clock=lambda: datetime.now(UTC))
    witness = witness_for(tenant) if witness_for else MemoryAuthorityWitness()
    store = PostgresHandoffAuthority(connect, tenant_id=tenant, witness=witness)
    coordinator = HandoffCoordinator(
        store=store,
        evidence=SignedObligationEvidence(
            current_envelope=PostgresSignedEvidence(connect, tenant_id=tenant).current_envelope,
            resolve_trust=registry.resolve, issuers={VERIFIER: signer.issuer_id},
            clock=lambda: datetime.now(UTC)),
        changes=changes,
        clock=lambda: datetime.now(UTC))
    counter = iter(range(10_000))

    def prove(obligation_id, result=VerificationResult.PASS, *, revisions=None,
              obligation=None):
        o = obligation or next(x for x in plan.obligations if x.obligation_id == obligation_id)
        observed = datetime.now(UTC) - timedelta(seconds=1) + timedelta(
            microseconds=next(counter))
        record = EvidenceRecord(
            f"ev-{uuid4().hex[:12]}", o.obligation_id, o.scope, revisions or plan.revisions,
            VERIFIER, observed, observed + timedelta(minutes=10), result,
            "observation", "a" * 64)
        signed.ingest(signer.sign(record, issued_at=observed))

    ops = json.loads(revision.canonical)["operations"]
    readiness = ExitReadiness(
        store=store, evidence=coordinator.evidence, changes=changes,
        clock=lambda: datetime.now(UTC), verifier_version=VERIFIER)
    return dict(revision=revision, plan=plan, store=store, coordinator=coordinator,
                readiness=readiness,
                claims=claims, prove=prove, ops=ops, app=app, tenant=tenant,
                connect=connect, lifecycle=lifecycle, world=world, feed=feed,
                trust_scopes=trust_scopes, changes=changes, verifier=VERIFIER,
                witness=witness, compiled=compiled,
                complete_feeds=lambda: complete_feeds(feed, revision, plan))
