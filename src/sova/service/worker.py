"""Workers that run admitted handoffs, any number of them at once.

A worker looks for authority records that are open and not held, takes the
execution lease of the handoff's operation, and runs it step by step: each
adapter step through the effect ledger (intent before the call, readback
after), each step's obligations measured and signed as evidence, and the
authority advanced only by the coordinator. Exactly one worker holds a
handoff at a time; the others skip it (``ExecutionBusy``) and take others.

While a step runs, a keeper thread renews the lease, so a step may take
longer than one lease (a large copy), but never longer than ``max_step_seconds``:
past that the keeper stops, the lease runs out, and liveness (no progress)
restarts the worker, whose successor recovers the step.

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
import time
from uuid import uuid4

from sova.adapters.postgres_compiled_plans import PostgresCompiledPlans
from sova.adapters.evidence_bounds import bounded
from sova.adapters.postgres_effects import PostgresEffectJournal
from sova.adapters.postgres_evidence_observations import PostgresEvidenceObservations
from sova.adapters.postgres_execution import PostgresExecutionOwnership
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
from sova.ports.operations import LeaseLost
from sova.service.control_plane import ControlPlane

log = logging.getLogger("sova.worker")
EVIDENCE_LIFETIME = timedelta(minutes=30)


@dataclass(frozen=True)
class Execution:
    """What a catalog builds for one handoff: the adapter of its manifest, the
    measurement of each obligation kind it can verify, and what ends it - the
    worker calls ``close`` once the steps it runs in a row are done (helpers
    kept between steps, such as volume agents, are let go)."""
    adapter: object
    measure: object  # (obligation, step_times) -> (passed, observation)
    close: object = None  # () -> None


def close_execution(execution):
    """End an execution; a failure to end it is logged, never raised over the
    step's own outcome."""
    if execution is not None and execution.close is not None:
        try:
            execution.close()
        except Exception:  # noqa: BLE001 - what it holds ends by itself in the end
            log.exception("ending an execution failed")


class AdapterCatalog:
    def __init__(self, factories):
        if type(factories) is not dict or not all(callable(f) for f in factories.values()):
            raise ContractError("Adapter factories by adapter ID required")
        self.factories = dict(factories)

    def build(self, tenant_id, application_id, adapter_id, revision):
        if adapter_id not in self.factories:
            raise ContractError(f"No adapter registered as {adapter_id}")
        return self.factories[adapter_id](tenant_id, application_id, revision)


class LeaseKeeper:
    """Renews an execution lease from a thread while one step runs."""

    def __init__(self, ownership, lease, *, lease_seconds, max_seconds):
        self.ownership, self.lease = ownership, lease
        self.lease_seconds, self.max_seconds = lease_seconds, max_seconds
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._keep, name="lease-keeper", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.thread.join(timeout=10)

    def _keep(self):
        until = time.monotonic() + self.max_seconds
        while not self.done.wait(max(1.0, self.lease_seconds / 3)):
            if time.monotonic() > until:
                log.error("step of %s ran past %ss; the lease is left to expire",
                          self.lease.operation_id, self.max_seconds)
                return
            try:
                self.ownership.renew(self.lease, lease_seconds=self.lease_seconds)
            except LeaseLost:
                log.error("lease of %s lost during a step", self.lease.operation_id)
                return
            except Exception:  # noqa: BLE001 - a database hiccup; the next try may pass
                log.exception("renewing the lease of %s failed", self.lease.operation_id)


def permit_for(command):
    return ExecutionPermit(command.scope, command.operation_id, command.plan_digest,
                           command.fingerprint, command.fencing_epoch, command.expires_at)


class HandoffWorker:
    def __init__(self, connect, *, settings, catalog, signer, clock, owner_id=None,
                 quiescence=None, lease_seconds=120, max_step_seconds=600):
        if not isinstance(catalog, AdapterCatalog):
            raise ContractError("An adapter catalog is required")
        self.connect, self.settings, self.catalog = connect, settings, catalog
        self.signer, self.clock = signer, clock
        self.owner_id = identifier(owner_id or "worker-" + uuid4().hex[:12], "owner_id")
        self.quiescence, self.lease_seconds = quiescence, lease_seconds
        if not lease_seconds < max_step_seconds:
            raise ContractError("A step may run longer than one lease, not shorter")
        self.max_step_seconds = max_step_seconds
        self.plane = ControlPlane(connect, settings=settings, clock=clock)
        self.stopping = threading.Event()
        self.progressed = time.monotonic()  # last poll or step boundary (liveness)

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
        # The observation is kept, under the digest the signed record names, so
        # what was measured can be read and checked against the signature.
        digest = PostgresEvidenceObservations(self.connect, tenant_id=tenant).record(
            application_id, o.obligation_id, bounded(observation, limit=60000))
        record = EvidenceRecord(
            f"ev-{uuid4().hex[:16]}", o.obligation_id, o.scope, plan.revisions,
            self.settings.evidence_verifier, observed, observed + EVIDENCE_LIFETIME,
            VerificationResult.PASS if passed else VerificationResult.FAIL, "observation",
            digest)
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
        try:
            effect = self._executor(ownership, recovery)[1].reconcile(
                command, permit_for(command), execution.adapter)
        finally:
            close_execution(execution)
        ownership.release(recovery)
        log.info("reconciled %s/%s step %s after a lost worker: %s", application_id, operation,
                 op["kind"], effect.status)
        lease = ownership.acquire(application_id, operation, self.owner_id,
                                  lease_seconds=self.lease_seconds)
        return lease, (step, effect)

    def _run(self, tenant, application_id, unit_digest, lifecycle, plan, revision, doc,
             operation, ownership, lease, coordinator, store, *, recovered=None):
        obligations = {o["id"]: o for o in doc["obligations"]}
        execution, opened = None, []  # opened: built by a step that may then fail
        try:
            while not self.stopping.is_set():
                self.progressed = time.monotonic()
                record = store.current(application_id, unit_digest)
                if record.state in _BLOCKED or record.next_operation >= len(doc["operations"]):
                    return record
                op = doc["operations"][record.next_operation]
                lease = ownership.renew(lease, lease_seconds=self.lease_seconds)
                with LeaseKeeper(ownership, lease, lease_seconds=self.lease_seconds,
                                 max_seconds=self.max_step_seconds):
                    record, recovered, execution, done = self._step(
                        tenant, application_id, lifecycle, plan, revision, doc, operation,
                        ownership, lease, coordinator, record, op, obligations, recovered,
                        execution, opened)
                if done:
                    return record
        finally:
            close_execution(execution if execution is not None else
                            (opened[-1] if opened else None))
        return store.current(application_id, unit_digest)

    def _step(self, tenant, application_id, lifecycle, plan, revision, doc, operation,
              ownership, lease, coordinator, record, op, obligations, recovered, execution,
              opened):
        """One step: its effect (or the recovered one), its measured evidence,
        the authority advanced. Returns (record, recovered, execution, stop); an
        execution it builds is also put in ``opened``, to be ended if it fails."""
        journal, engine = self._executor(ownership, lease)
        effect = None
        if recovered is not None and recovered[0] == record.next_operation:
            # Reconciled after a lost worker: its readback decides, nothing re-runs.
            effect, recovered = recovered[1], None
            if execution is None:
                execution = self.catalog.build(tenant, application_id,
                                               op["adapter"]["adapter_id"], revision)
                opened.append(execution)
        elif op["adapter"]:
            if execution is None:
                execution = self.catalog.build(tenant, application_id,
                                               op["adapter"]["adapter_id"], revision)
                opened.append(execution)
            command = coordinator.next_command(revision, plan, operation_id=operation,
                                               epoch=lease.epoch)
            try:
                effect = engine.execute(command, permit_for(command), execution.adapter)
            except UncertainEffect:
                held = journal.get(command)
                return (coordinator.advance_from_ledger(revision, plan, effect=held,
                                                        actor_id=self.owner_id),
                        recovered, execution, True)
        elif execution is None and any(
                obligations[i]["required_at"] == op["operation_id"]
                for i in op["required_evidence"]):
            adapter_op = next(o for o in doc["operations"] if o["adapter"])
            execution = self.catalog.build(tenant, application_id,
                                           adapter_op["adapter"]["adapter_id"], revision)
            opened.append(execution)
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
        return record, recovered, execution, record.state == HandoffState.SAFE_HOLD

    # -- the process -----------------------------------------------------------------------
    def run(self, tenants, *, poll_seconds=5.0, ready=None):
        """Serve ``tenants`` until ``stopping`` is set (SIGTERM): finish the
        current step, release the lease, take nothing new. While ``ready()``
        is false (the schema is not yet this release's) nothing is taken."""
        while not self.stopping.is_set():
            self.progressed = time.monotonic()
            try:
                waiting = ready is not None and not ready()
            except Exception:  # noqa: BLE001 - the database may be back next pass
                log.exception("readiness check failed")
                waiting = True
            if waiting:
                log.info("waiting for the schema of this release")
                self.stopping.wait(poll_seconds)
                continue
            for tenant in tenants:
                try:
                    units = self.open_units(tenant)
                except Exception:  # noqa: BLE001 - the database may be back next pass
                    log.exception("listing open handoffs of %s failed", tenant)
                    continue
                for application_id, unit in units:
                    if self.stopping.is_set():
                        return
                    try:
                        self.drive(tenant, application_id, unit)
                    except Exception:  # noqa: BLE001 - one handoff never stops the others
                        log.exception("handoff %s/%s failed this pass", application_id, unit)
            self.stopping.wait(poll_seconds)
