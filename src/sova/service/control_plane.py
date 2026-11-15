"""The control plane's operations for one tenant, as a service exposes them.

Every operation takes the calling ``Principal`` and checks its role here, not
only at the API: a worker, a CLI and the HTTP layer all reach the same rules.

* register   (registrar)  a registry document, compare-and-set by revision
* attest     (attestor)   a signed registry attestation, verified on ingest
* compile    (planner)    a plan from registered subjects' current revisions
* approve    (approver)   never by the principal who compiled the plan
* admit      (operator)   recompile from the same request, claim, open authority
* status     (auditor or any role of the tenant)
* abort      (operator)   before activation only, as the authority allows

Nothing here executes an effect: admitted handoffs are run by workers holding
an execution lease (``sova.service.worker``).
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
import json

from sova.adapters.ed25519_evidence import Ed25519EvidenceVerifier
from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.postgres_handoff_authority import PostgresHandoffAuthority
from sova.adapters.postgres_handoff_claims import PostgresHandoffClaims
from sova.adapters.postgres_operations import PostgresOperationRepository
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_registrations import PostgresRegistrations
from sova.adapters.postgres_signed_evidence import PostgresSignedEvidence
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.adapters.registration import registry_codec as codecs
from sova.adapters.signed_evidence_codec import decode_envelope
from sova.application.change_monitor import ChangeImpactSource
from sova.application.handoff_admission import HandoffAdmission
from sova.application.handoff_coordinator import HandoffCoordinator, SignedObligationEvidence
from sova.application.registry_authentication import postgres_registry_authenticator
from sova.application.signed_evidence import SignedEvidenceAuthority
from sova.domain.contracts import ContractError, identifier, utc
from sova.domain.operations import OperationRequest
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan
from sova.domain.plans import PlanApproval, PlanPurpose


class Role(StrEnum):
    REGISTRAR = "registrar"
    ATTESTOR = "attestor"
    PLANNER = "planner"
    APPROVER = "approver"
    OPERATOR = "operator"
    AUDITOR = "auditor"


class Forbidden(ContractError):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset

    def __post_init__(self):
        identifier(self.subject, "subject")
        identifier(self.tenant_id, "tenant_id")
        if not all(isinstance(r, Role) for r in self.roles):
            raise ContractError("Typed roles required")


@dataclass(frozen=True, slots=True)
class PlanRequest:
    service_id: str
    snapshot_id: str
    source_profile: str
    target_profile: str
    manifest: str
    qualification: str
    nodes: tuple
    rows: int

    @classmethod
    def from_document(cls, data):
        fields = ("service_id", "snapshot_id", "source_profile", "target_profile",
                  "manifest", "qualification", "nodes", "rows")
        if type(data) is not dict or set(data) != set(fields):
            raise ContractError("Plan request fields do not match the schema")
        if type(data["nodes"]) is not list or not data["nodes"]:
            raise ContractError("At least one requested node")
        if type(data["rows"]) is not int or data["rows"] < 1:
            raise ContractError("A positive row bound")
        return cls(*(data[f] for f in fields[:6]), tuple(sorted(data["nodes"])), data["rows"])

    @property
    def document(self):
        return {"service_id": self.service_id, "snapshot_id": self.snapshot_id,
                "source_profile": self.source_profile, "target_profile": self.target_profile,
                "manifest": self.manifest, "qualification": self.qualification,
                "nodes": list(self.nodes), "rows": self.rows}


@dataclass(frozen=True, slots=True)
class Settings:
    registry_verifier_version: str
    evidence_verifier: str          # verifier identity named in every obligation
    evidence_issuer: str            # the issuer whose signatures that verifier stands for
    witness_for: object             # tenant_id -> authority witness
    max_approval: timedelta = timedelta(hours=4)


class ControlPlane:
    def __init__(self, connect, *, settings, clock):
        if not callable(connect) or not callable(clock):
            raise ContractError("Connection factory and trusted clock required")
        self.connect, self.settings, self.clock = connect, settings, clock

    # -- plumbing ------------------------------------------------------------------
    def _require(self, principal, *roles):
        if not isinstance(principal, Principal) or not principal.roles & set(roles):
            raise Forbidden("The caller lacks the role this operation needs")
        return principal.tenant_id

    def _lifecycle(self, tenant):
        return PostgresLifecycleRepository(self.connect, tenant_id=tenant, clock=self.clock)

    def _registrations(self, tenant):
        return PostgresRegistrations(self.connect, tenant_id=tenant)

    def _verifiers(self, revision):
        kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
        return {k: self.settings.evidence_verifier for k in kinds}

    def _changes(self, tenant):
        return ChangeImpactSource(feed=PostgresChangeFeed(self.connect, tenant_id=tenant),
                                  clock=self.clock)

    def _coordinator(self, tenant, application_id):
        registry = PostgresVerifierRegistry(self.connect, tenant_id=tenant,
                                            application_id=application_id)
        store = PostgresHandoffAuthority(self.connect, tenant_id=tenant,
                                         witness=self.settings.witness_for(tenant))
        evidence = SignedObligationEvidence(
            current_envelope=PostgresSignedEvidence(self.connect,
                                                    tenant_id=tenant).current_envelope,
            resolve_trust=registry.resolve,
            issuers={self.settings.evidence_verifier: self.settings.evidence_issuer},
            clock=self.clock)
        return HandoffCoordinator(store=store, evidence=evidence,
                                  changes=self._changes(tenant), clock=self.clock), store

    # -- registry ------------------------------------------------------------------
    def register(self, principal, kind, subject_id, document, *, expected_revision):
        tenant = self._require(principal, Role.REGISTRAR)
        return self._registrations(tenant).register(
            kind, subject_id, document, expected_revision=expected_revision,
            registered_by=principal.subject)

    def registration(self, principal, kind, subject_id):
        tenant = self._require(principal, *Role)
        return self._registrations(tenant).current(kind, subject_id)

    def attest(self, principal, envelope_document):
        """Ingest a signed registry attestation; its signature and the signer's
        current trust for that application are verified before it is stored."""
        tenant = self._require(principal, Role.ATTESTOR)
        envelope = decode_envelope(envelope_document)
        scope = envelope.evidence.scope
        if scope.tenant_id != tenant:
            raise Forbidden("The attestation belongs to another tenant")
        registry = PostgresVerifierRegistry(self.connect, tenant_id=tenant,
                                            application_id=scope.application_id)
        SignedEvidenceAuthority(PostgresSignedEvidence(self.connect, tenant_id=tenant),
                                Ed25519EvidenceVerifier(registry.resolve),
                                clock=self.clock).ingest(envelope)
        return {"evidence_id": envelope.evidence.evidence_id, "scope": {
            "application_id": scope.application_id, "environment_id": scope.environment_id,
            "resource_id": scope.resource_id}}

    # -- planning ------------------------------------------------------------------
    def _recompile(self, tenant, request):
        r = self._registrations(tenant)

        def current(kind, subject):
            found = r.current(kind, subject)
            if found is None:
                raise ContractError(f"Nothing registered as {kind} {subject}")
            return found.value

        authenticate = postgres_registry_authenticator(
            self.connect, tenant_id=tenant, application_id=request.service_id,
            verifier_version=self.settings.registry_verifier_version, clock=self.clock)
        return compile_revision(
            current("service_contract", request.service_id),
            current("dependency_snapshot", request.snapshot_id),
            requested_nodes=request.nodes, expected_tenant=tenant,
            source=current("environment_profile", request.source_profile),
            target=current("environment_profile", request.target_profile),
            manifest=current("capability_manifest", request.manifest),
            qualification=current("strategy_qualification", request.qualification),
            rows=request.rows, authenticate=authenticate, clock=self.clock)

    def compile(self, principal, request):
        tenant = self._require(principal, Role.PLANNER)
        if not isinstance(request, PlanRequest):
            raise ContractError("A typed plan request is required")
        revision, result = self._recompile(tenant, request)
        if result["status"] != "COMPILED":
            return {"status": result["status"], "blockers": result["blockers"]}
        plan = lower_plan(revision, created_by=principal.subject,
                          verifiers=self._verifiers(revision))
        self._lifecycle(tenant).save_plan(plan)
        with self._lifecycle(tenant).transaction() as conn:
            conn.execute(
                """INSERT INTO sova_control.plan_requests
                (tenant_id,application_id,plan_id,compiled_digest,request,requested_by)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (tenant, plan.application_id, plan.plan_id, revision.digest,
                 json.dumps(request.document, sort_keys=True), principal.subject))
        PostgresCompiledPlans(self.connect, tenant_id=tenant).save(revision)
        return {"status": "COMPILED", "application_id": plan.application_id,
                "plan_id": plan.plan_id, "plan_digest": plan.fingerprint,
                "compiled_digest": revision.digest, "blockers": result["blockers"]}

    def _request(self, tenant, application_id, plan_id):
        with self._lifecycle(tenant).transaction() as conn:
            row = conn.execute(
                """SELECT request, compiled_digest FROM sova_control.plan_requests
                WHERE tenant_id=%s AND application_id=%s AND plan_id=%s""",
                (tenant, application_id, plan_id)).fetchone()
        if row is None:
            raise ContractError("No plan request for this plan")
        return PlanRequest.from_document(json.loads(row[0])), row[1]

    # -- approval and admission -------------------------------------------------------
    def approve(self, principal, application_id, plan_id, approval_id, *,
                purpose="migration_phase", valid_for=timedelta(hours=1)):
        tenant = self._require(principal, Role.APPROVER)
        if not timedelta(0) < valid_for <= self.settings.max_approval:
            raise ContractError("Approval validity out of bounds")
        lifecycle = self._lifecycle(tenant)
        plan = lifecycle.get_plan(application_id, plan_id)
        if plan.created_by == principal.subject:
            raise Forbidden("Separation of duties: a plan is approved by someone else")
        lifecycle.save_approval(application_id, plan_id, approval_id, PlanApproval(
            plan.fingerprint, principal.subject, PlanPurpose(purpose),
            utc(self.clock()) + valid_for))
        return {"approval_id": approval_id, "plan_id": plan_id}

    def admit(self, principal, application_id, approval_id):
        """Admit an approved plan (the authority is opened in SOURCE_ACTIVE) and
        submit its operation for a worker; returns the unit to watch."""
        tenant = self._require(principal, Role.OPERATOR)
        lifecycle = self._lifecycle(tenant)
        approval = lifecycle.get_approval(application_id, approval_id)
        request, compiled_digest = self._request(tenant, application_id, approval.plan_id)
        compiled = PostgresCompiledPlans(self.connect, tenant_id=tenant)
        revision = compiled.load(compiled_digest)

        def recompile():
            return self._recompile(tenant, request)

        HandoffAdmission(lifecycle=lifecycle,
                         claims=PostgresHandoffClaims(self.connect, tenant_id=tenant),
                         changes=self._changes(tenant), compiled=compiled,
                         clock=self.clock).admit(application_id, approval_id,
                                                 recompile=recompile,
                                                 verifiers=self._verifiers(revision))
        plan = lifecycle.get_plan(application_id, approval.plan_id)
        coordinator, _ = self._coordinator(tenant, application_id)
        coordinator.open(revision, plan, actor_id=principal.subject)
        operation = "handoff-" + plan.plan_id[-24:]
        PostgresOperationRepository(lifecycle).submit(OperationRequest(
            tenant, application_id, operation, plan.plan_id, plan.fingerprint, approval_id,
            approval.revision, principal.subject, operation + "-request"))
        return {"application_id": application_id, "plan_id": plan.plan_id,
                "operation_id": operation, "unit_digest": json.loads(
                    revision.canonical)["unit"]["unit_digest"]}

    # -- observation and abort ----------------------------------------------------------
    def status(self, principal, application_id, unit_digest):
        tenant = self._require(principal, *Role)
        _, store = self._coordinator(tenant, application_id)
        record = store.current(application_id, unit_digest)
        if record is None:
            return {"state": None}
        return {"state": str(record.state), "epoch": record.epoch,
                "owner_environment": record.owner_environment,
                "next_operation": record.next_operation, "reason": record.reason,
                "activation_possible": record.activation_possible,
                "history": [{"revision": rev, "action": action, "actor": actor,
                             "state": str(rec.state)}
                            for rev, action, actor, rec in store.history(application_id,
                                                                         unit_digest)]}

    def abort(self, principal, application_id, plan_id, *, target_inactive_verified):
        tenant = self._require(principal, Role.OPERATOR)
        lifecycle = self._lifecycle(tenant)
        plan = lifecycle.get_plan(application_id, plan_id)
        _, compiled_digest = self._request(tenant, application_id, plan_id)
        revision = PostgresCompiledPlans(self.connect, tenant_id=tenant).load(compiled_digest)
        coordinator, _ = self._coordinator(tenant, application_id)
        record = coordinator.abort(revision, plan,
                                   target_inactive_verified=target_inactive_verified,
                                   actor_id=principal.subject)
        return {"state": str(record.state)}

    def kinds(self):
        return codecs.KINDS
