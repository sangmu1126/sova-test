"""Driving a compiled handoff through the real control plane, shared by the
local and the cloud-to-on-premises tests: approval, admission, authority record,
next-step commands through the effect ledger, measured probes and signed
evidence decide every step."""

from datetime import UTC, datetime, timedelta
import json

from handoff_support import admitted_handoff
from postgres_business import PROBE_INSERT, SCHEMA, TABLES, Route
import pytest
from support import permit_for

from sova.adapters.postgres_effects import PostgresEffectJournal
from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes, artifact_digest
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.adapters.postgres_operations import PostgresOperationRepository
from sova.application.effects import EffectExecutor, UncertainEffect
from sova.application.execution_recovery import ExecutionRecovery
from sova.domain.contracts import EffectStatus
from sova.domain.evidence import VerificationResult
from sova.domain.execution import QuiescenceProof, pending_digest
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.domain.operations import OperationRequest
from sova.ports.execution import RecoveryRequired

OPERATION = "real-handoff"
class LosesResponse:
    """The effect happens, the executor never hears back (AT-20)."""

    def __init__(self, inner):
        self.inner, self.adapter_id = inner, inner.adapter_id

    def validate(self, command):
        self.inner.validate(command)

    def apply(self, command):
        self.inner.apply(command)
        raise ConnectionError("response lost after the change was made")

    def readback(self, command, now):
        return self.inner.readback(command, now)


def drive(h, revision, plan, *, adapter, probes, other_db, scratch_db, operation,
          approval_id, until=None, before=None, lose_response_at=(), events=None,
          after_effect=None):
    """Run a compiled plan's steps through the ledger with measured evidence."""
    lifecycle = h["lifecycle"]
    approval = lifecycle.get_approval(h["app"], approval_id)
    PostgresOperationRepository(lifecycle).submit(OperationRequest(
        h["tenant"], h["app"], operation, plan.plan_id, plan.fingerprint,
        approval_id, approval.revision, "operator", operation + "-request"))
    ownership = PostgresExecutionOwnership(lifecycle)

    def executor_for(lease):
        journal = PostgresEffectJournal(ownership, lease)
        return journal, EffectExecutor(journal, authorize=journal.authorize,
                                       clock=lambda: datetime.now(UTC))

    lease = ownership.acquire(h["app"], operation, "worker")
    journal, engine = executor_for(lease)
    h["coordinator"].open(revision, plan, actor_id="coordinator")
    contract = h["world"]["contract"].document["spec"]
    doc = json.loads(revision.canonical)
    observed = {}  # step kind -> ledger observation time

    def measure(obligation):
        kind, oid = obligation["kind"], obligation["id"]
        if kind == "placement_evidence":
            return probes.zone("target", set(contract["residency"][oid.split(".", 1)[1]]))
        if kind == "return_target_check":
            return probes.write_denied("target")
        if oid == "fence.source_writes_denied" or kind == "stale_route_probe":
            return probes.write_denied("source")
        if kind == "final_content_comparison":
            return probes.content_equal(oid.split(".", 1)[1])
        if kind == "business_invariant":
            return probes.order_effects_once()
        if oid == "security.deny_cross_tenant_data_access":
            return probes.cross_tenant_denied(other_db)
        if oid == "security.verified_transport_encryption":
            return probes.transport_encrypted("target")
        if kind == "restore_drill" or oid == "security.tested_restore":
            return probes.restore_drill(scratch_db)
        if kind == "authority_readback":
            return probes.single_writer()
        if kind == "measured_budget":
            end = "activate_target" if oid.endswith("write_admission_pause") else "commit_route"
            spent = (observed[end] - observed["pause_admission"]).total_seconds() * 1000
            return spent <= obligation["budget_ms"], {
                "probe": "measured_budget", "spent_ms": round(spent, 3),
                "budget_ms": obligation["budget_ms"]}
        raise AssertionError(f"no probe for {oid}")

    obligations = {o["id"]: o for o in doc["obligations"]}
    record = None
    for op in doc["operations"]:
        if op["kind"] == until:
            break
        if before:
            before(op["kind"])
        effect = None
        if op["adapter"]:
            command = h["coordinator"].next_command(revision, plan, operation_id=operation,
                                                    epoch=lease.epoch)
            if op["kind"] in lose_response_at:
                with pytest.raises(UncertainEffect):
                    engine.execute(command, permit_for(command), LosesResponse(adapter))
                held = h["coordinator"].advance_from_ledger(
                    revision, plan, effect=journal.get(command), actor_id="coordinator")
                events.append((op["kind"], str(held.state), held.reason,
                               held.activation_possible))
                # The executor that lost the response is gone; its lease ends.
                with lifecycle.transaction() as conn:
                    conn.execute(
                        "UPDATE sova_control.app_execution SET valid_until="
                        "clock_timestamp()-interval '1 second' WHERE application_id=%s",
                        (h["app"],))
                    at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                    pending = ownership.pending(conn, h["app"])
                # Recovery lease bound to exactly the pending effect (existing rule).
                recovery = ExecutionRecovery(ownership, authenticate_proof=lambda _: None)\
                    .acquire(QuiescenceProof(h["tenant"], h["app"], operation, lease.epoch,
                                             pending_digest(pending), "e" * 64, at,
                                             at + timedelta(minutes=1)), "recovery-worker")
                # Read-only reconciliation: look at the databases again, never re-run.
                effect = executor_for(recovery)[1].reconcile(command, permit_for(command),
                                                             adapter)
                ownership.release(recovery)
                lease = ownership.acquire(h["app"], operation, "worker-after-recovery")
                journal, engine = executor_for(lease)
                observed[op["kind"]] = effect.observation.observed_at
                for oid in op["required_evidence"]:
                    passed, observation = measure(obligations[oid])
                    h["prove"](oid, VerificationResult.PASS if passed else
                               VerificationResult.FAIL,
                               artifact_digest=artifact_digest(observation),
                               obligation=next(o for o in plan.obligations
                                               if o.obligation_id == oid),
                               revisions=plan.revisions)
                record = h["coordinator"].reconcile(
                    revision, plan, outcome=EffectOutcome.EFFECT_CONFIRMED
                    if effect.status == EffectStatus.APPLIED else EffectOutcome.EFFECT_UNKNOWN,
                    actor_id="coordinator")
                events.append((op["kind"], str(record.state), record.reason, None))
                continue
            try:
                effect = engine.execute(command, permit_for(command), adapter)
            except UncertainEffect:
                # The readback did not confirm the postcondition: hold, do not guess.
                record = h["coordinator"].advance_from_ledger(
                    revision, plan, effect=journal.get(command), actor_id="coordinator")
                break
            observed[op["kind"]] = effect.observation.observed_at
            if after_effect:
                after_effect(op)
        for oid in op["required_evidence"]:
            passed, observation = measure(obligations[oid])
            h["prove"](oid, VerificationResult.PASS if passed else VerificationResult.FAIL,
                       artifact_digest=artifact_digest(observation),
                       obligation=next(o for o in plan.obligations if o.obligation_id == oid),
                       revisions=plan.revisions)
        record = h["coordinator"].advance_from_ledger(revision, plan, effect=effect,
                                                      actor_id="coordinator")
        if record.state == HandoffState.SAFE_HOLD:
            break
    # This executor is done; ownership is released only with no unresolved effect.
    try:
        ownership.release(lease)
    except RecoveryRequired:
        pass  # an unknown effect keeps the app in recovery, as the ledger requires
    return record, observed


def pieces(business, adapter_id, *, source, target, route, fenced=None):
    """The adapter fences ``fenced`` writer roles (default: every registered one);
    the probes always check every writer path that really exists."""
    names, app = business["names"], business["app"]
    writers = (app, business["batch"])
    adapter = PostgresOfflineCopyAdapter(
        adapter_id, connect=business["connect"], source_db=names[source],
        target_db=names[target], app_role=app, tables=TABLES, schema_sql=SCHEMA, route=route,
        writer_roles=fenced or writers)
    probes = PostgresHandoffProbes(
        connect_as=business["connect_as"], admin_connect=business["connect"],
        source_db=names[source], target_db=names[target], app_role=app, tables=TABLES,
        probe_insert=PROBE_INSERT, writer_roles=writers)
    return adapter, probes


def run_handoff(postgres_database, now, business, *, adjust=None, until=None,
                strategy="offline-copy", before=None, lose_response_at=(), events=None,
                fenced=None, watch_catalog=None):
    h = admitted_handoff(postgres_database, now, adjust=adjust, strategy=strategy)
    after_effect = None
    if watch_catalog:
        before, after_effect = watch_catalog(h, before)
    names = business["names"]
    route = Route(names["src"])
    adapter, probes = pieces(business, h["world"]["manifest"].adapter_id,
                             source="src", target="tgt", route=route, fenced=fenced)
    record, observed = drive(h, h["revision"], h["plan"], adapter=adapter, probes=probes,
                             other_db=names["other"], scratch_db=names["scratch"],
                             operation=OPERATION, approval_id="approval-1", until=until,
                             before=before, lose_response_at=lose_response_at,
                             events=events, after_effect=after_effect)
    return dict(h, record=record, adapter=adapter, probes=probes, route=route,
                business=business, observed=observed)


def without_transport_control(data):
    """The local reference profile: no TLS on the disposable server, and budgets
    wide enough that a loaded test machine does not decide the outcome."""
    controls = data["spec"]["security"]["required_controls"]
    data["spec"]["security"]["required_controls"] = [
        c for c in controls if c != "verified_transport_encryption"]
    data["spec"]["availability"]["max_write_admission_pause_ms"] = 60000
    data["spec"]["availability"]["max_client_completion_wait_ms"] = 120000
