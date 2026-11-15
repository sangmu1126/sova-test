"""Workers that run admitted handoffs, any number of them at once.

A worker looks for authority records that are open and not held, takes the
execution lease of the handoff's operation, and runs it step by step: each
adapter step through the effect ledger (intent before the call, readback
after), each step's obligations measured and signed as evidence, and the
authority advanced only by the coordinator. Exactly one worker holds a
handoff at a time; the others skip it (``ExecutionBusy``) and take others.

When the holder dies, its lease expires. If it died between steps, the next
worker simply takes the lease. If it died during a step, the ledger has an
effect in flight and the database asks for recovery: a recovery lease is
granted only on a ``QuiescenceProof`` from ``quiescence`` (in Kubernetes:
the old worker's pod, by name and UID, no longer runs), then the step is
reconciled by a read-only readback, never re-run, and work continues.

Holds (SAFE_HOLD and the other blocked states) are not touched: they wait
for people.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import threading
from uuid import uuid4

from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.postgres_effects import PostgresEffectJournal
from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.adapters.postgres_handoff_probes import artifact_digest
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_signed_evidence import PostgresSignedEvidence
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.adapters.ed25519_evidence import Ed25519EvidenceVerifier
from sova.application.effects import EffectExecutor, UncertainEffect
from sova.application.execution_recovery import ExecutionRecovery
from sova.application.handoff_coordinator import _BLOCKED
from sova.application.signed_evidence import SignedEvidenceAuthority
from sova.domain.contracts import ContractError, ExecutionPermit, identifier, utc
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.handoff_authority import HandoffState
from sova.ports.execution import ExecutionBusy, RecoveryRequired
from sova.service.control_plane import ControlPlane

log = logging.getLogger("sova.worker")
EVIDENCE_LIFETIME = timedelta(minutes=30)


@dataclass(frozen=True)
class Execution:
    """What a catalog builds for one handoff: the adapter of its manifest and
    the measurement of each obligation kind it can verify."""
    adapter: object
    measure: object  # (obligation, step_times) -> (passed, observation)


class AdapterCatalog:
    def __init__(self, factories):
        if type(factories) is not dict or not all(callable(f) for f in factories.values()):
            raise ContractError("Adapter factories by adapter ID required")
        self.factories = dict(factories)

    def build(self, tenant_id, application_id, adapter_id, revision):
        if adapter_id not in self.factories:
            raise ContractError(f"No adapter registered as {adapter_id}")
        return self.factories[adapter_id](tenant_id, application_id, revision)


def permit_for(command):
    return ExecutionPermit(command.scope, command.operation_id, command.plan_digest,
                           command.fingerprint, command.fencing_epoch, command.expires_at)


class HandoffWorker:
    def __init__(self, connect, *, settings, catalog, signer, clock, owner_id=None,
                 quiescence=None, lease_seconds=120):
        if not isinstance(catalog, AdapterCatalog):
            raise ContractError("An adapter catalog is required")
        self.connect, self.settings, self.catalog = connect, settings, catalog
        self.signer, self.clock = signer, clock
        self.owner_id = identifier(owner_id or "worker-" + uuid4().hex[:12], "owner_id")
        self.quiescence, self.lease_seconds = quiescence, lease_seconds
        self.plane = ControlPlane(connect, settings=settings, clock=clock)
        self.stopping = threading.Event()

    # -- finding work ------------------------------------------------------------------
    def open_units(self, tenant):
        """(application, unit digest) of every handoff a worker may advance now."""
        lifecycle = PostgresLifecycleRepository(self.connect, tenant_id=tenant, clock=self.clock)
        blocked = sorted(str(s) for s in _BLOCKED)
        with lifecycle.transaction() as conn:
            rows = conn.execute(
                """SELECT application_id, unit_digest FROM sova_control.handoff_authority
                WHERE tenant_id=%s AND NOT (state = ANY(%s)) ORDER BY updated_at""",
                (tenant, blocked)).fetchall()
        return [tuple(r) for r in rows]

    def _plan_of(self, tenant, application_id, plan_digest):
        lifecycle = PostgresLifecycleRepository(self.connect, tenant_id=tenant, clock=self.clock)
        with lifecycle.transaction() as conn:
            row = conn.execute(
                """SELECT plan_id FROM sova_control.plans WHERE tenant_id=%s
                AND application_id=%s AND digest=%s""",
                (tenant, application_id, plan_digest)).fetchone()
        if row is None:
            raise ContractError("The admitted plan is not stored")
        return lifecycle, lifecycle.get_plan(application_id, row[0])

    def _step_times(self, lifecycle, tenant, application_id, operation):
        """When each adapter step was observed applied, from the effect ledger
        (so a budget is measured the same after a worker restart)."""
        with lifecycle.transaction() as conn:
            rows = conn.execute(
                """SELECT command_document->>'action', observation_document->>'observed_at'
                FROM sova_control.effects WHERE tenant_id=%s AND application_id=%s
                AND operation_id=%s AND status='confirmed_applied'""",
                (tenant, application_id, operation)).fetchall()
        return {action: datetime.fromisoformat(at) for action, at in rows if action and at}

    # -- evidence ----------------------------------------------------------------------
    def _prove(self, tenant, application_id, plan, obligation_id, passed, observation):
        o = next(x for x in plan.obligations if x.obligation_id == obligation_id)
        observed = utc(self.clock()) - timedelta(milliseconds=1)
        record = EvidenceRecord(
            f"ev-{uuid4().hex[:16]}", o.obligation_id, o.scope, plan.revisions,
            self.settings.evidence_verifier, observed, observed + EVIDENCE_LIFETIME,
            VerificationResult.PASS if passed else VerificationResult.FAIL, "observation",
            artifact_digest(observation))
        registry = PostgresVerifierRegistry(self.connect, tenant_id=tenant,
                                            application_id=application_id)
        SignedEvidenceAuthority(PostgresSignedEvidence(self.connect, tenant_id=tenant),
                                Ed25519EvidenceVerifier(registry.resolve),
                                clock=self.clock).ingest(self.signer.sign(record,
                                                                          issued_at=observed))

    # -- one handoff ---------------------------------------------------------------------
    def drive(self, tenant, application_id, unit_digest):
        """Advance one handoff as far as it goes now; returns its authority record,
        or None when another worker holds it."""
        coordinator, store = self.plane._coordinator(tenant, application_id)
        record = store.current(application_id, unit_digest)
        if record is None or record.state in _BLOCKED:
            return record
        lifecycle, plan = self._plan_of(tenant, application_id, record.plan_digest)
        revision = PostgresCompiledPlans(self.connect, tenant_id=tenant).load(
            record.compiled_digest)
        doc = json.loads(revision.canonical)
        operation = "handoff-" + plan.plan_id[-24:]
        ownership = PostgresExecutionOwnership(lifecycle)
        taken = self._lease(ownership, lifecycle, tenant, application_id, operation,
                            coordinator, revision, plan, doc)
        if taken is None:
            return None
        lease, recovered = taken
        try:
            return self._run(tenant, application_id, unit_digest, lifecycle, plan, revision,
                             doc, operation, ownership, lease, coordinator, store,
                             recovered=recovered)
        finally:
            try:
                ownership.release(lease)
            except RecoveryRequired:
                pass  # an unknown effect keeps it in recovery, as the ledger requires

    def _executor(self, ownership, lease):
        journal = PostgresEffectJournal(ownership, lease)
        return journal, EffectExecutor(journal, authorize=journal.authorize, clock=self.clock)

    def _lease(self, ownership, lifecycle, tenant, application_id, operation, coordinator,
               revision, plan, doc):
        try:
            return ownership.acquire(application_id, operation, self.owner_id,
                                     lease_seconds=self.lease_seconds), None
        except ExecutionBusy:
            return None
        except RecoveryRequired:
            pass
        # A worker died during a step. Recover only on proof that it is gone.
        if self.quiescence is None:
            return None
        with lifecycle.transaction() as conn:
            pending = ownership.pending(conn, application_id)
        proof = self.quiescence.prove(lifecycle, tenant, application_id, operation, pending)
        if proof is None:
            return None
        recovery = ExecutionRecovery(ownership, authenticate_proof=self.quiescence.authenticate)\
            .acquire(proof, self.owner_id, lease_seconds=self.lease_seconds)
        step = coordinator._current(revision, plan)[1].next_operation
        op = doc["operations"][step]
        execution = self.catalog.build(tenant, application_id, op["adapter"]["adapter_id"],
                                       revision)
        # The same command the lost worker issued (its epoch), read back only.
        command = coordinator.next_command(revision, plan, operation_id=operation,
                                           epoch=proof.source_epoch)
        effect = self._executor(ownership, recovery)[1].reconcile(command, permit_for(command),
                                                                  execution.adapter)
        ownership.release(recovery)
        log.info("reconciled %s/%s step %s after a lost worker: %s", application_id, operation,
                 op["kind"], effect.status)
        lease = ownership.acquire(application_id, operation, self.owner_id,
                                  lease_seconds=self.lease_seconds)
        return lease, (step, effect)

    def _run(self, tenant, application_id, unit_digest, lifecycle, plan, revision, doc,
             operation, ownership, lease, coordinator, store, *, recovered=None):
        obligations = {o["id"]: o for o in doc["obligations"]}
        execution = None
        while not self.stopping.is_set():
            record = store.current(application_id, unit_digest)
            if record.state in _BLOCKED or record.next_operation >= len(doc["operations"]):
                return record
            op = doc["operations"][record.next_operation]
            lease = ownership.renew(lease, lease_seconds=self.lease_seconds)
            journal, engine = self._executor(ownership, lease)
            effect = None
            if recovered is not None and recovered[0] == record.next_operation:
                # Reconciled after a lost worker: its readback decides, nothing re-runs.
                effect, recovered = recovered[1], None
                if execution is None:
                    execution = self.catalog.build(tenant, application_id,
                                                   op["adapter"]["adapter_id"], revision)
            elif op["adapter"]:
                if execution is None:
                    execution = self.catalog.build(tenant, application_id,
                                                   op["adapter"]["adapter_id"], revision)
                command = coordinator.next_command(revision, plan, operation_id=operation,
                                                   epoch=lease.epoch)
                try:
                    effect = engine.execute(command, permit_for(command), execution.adapter)
                except UncertainEffect:
                    held = journal.get(command)
                    return coordinator.advance_from_ledger(revision, plan, effect=held,
                                                           actor_id=self.owner_id)
            elif execution is None and any(
                    obligations[i]["required_at"] == op["operation_id"]
                    for i in op["required_evidence"]):
                adapter_op = next(o for o in doc["operations"] if o["adapter"])
                execution = self.catalog.build(tenant, application_id,
                                               adapter_op["adapter"]["adapter_id"], revision)
            times = None
            for oid in op["required_evidence"]:
                if obligations[oid]["required_at"] != op["operation_id"]:
                    continue  # measured at an earlier step; re-checked, not re-taken
                if times is None:
                    times = self._step_times(lifecycle, tenant, application_id, operation)
                passed, observation = execution.measure(obligations[oid], times)
                self._prove(tenant, application_id, plan, oid, passed, observation)
            record = coordinator.advance_from_ledger(revision, plan, effect=effect,
                                                     actor_id=self.owner_id)
            if record.state == HandoffState.SAFE_HOLD:
                return record
        return store.current(application_id, unit_digest)

    # -- the process -----------------------------------------------------------------------
    def run(self, tenants, *, poll_seconds=5.0):
        """Serve ``tenants`` until ``stopping`` is set (SIGTERM): finish the
        current step, release the lease, take nothing new."""
        while not self.stopping.is_set():
            for tenant in tenants:
                for application_id, unit in self.open_units(tenant):
                    if self.stopping.is_set():
                        return
                    try:
                        self.drive(tenant, application_id, unit)
                    except Exception:  # noqa: BLE001 - one handoff never stops the others
                        log.exception("handoff %s/%s failed this pass", application_id, unit)
            self.stopping.wait(poll_seconds)
