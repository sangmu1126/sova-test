"""Apply authenticated observations to a unit's persisted authority record.

Evidence satisfaction is never taken from the caller: for each obligation an
operation requires, the coordinator reads the latest signed observation on the
registered producer channel, verifies its signature and current issuer trust,
and decides it with the existing ``evaluate_gate`` binding rules (obligation,
scope, plan revisions, verifier version, freshness). Handoff steps accept PASS
only; a NOT_APPLICABLE exception does not satisfy a safety step.

``advance_from_ledger`` takes the outcome from the existing effect ledger's
record for exactly the next planned step; ``next_command`` issues a command
only for that step. Platform-owned steps (activation intent, readiness,
stabilization) have no external effect and advance on evidence alone.

Change impact (INV-08) applies to every decision: evidence observed at or
before a change that affects its obligation does not count, and while the
plan's inputs are stale or a change feed has a gap, no command is issued and
no platform-owned step (such as recording activation intent) advances. An
adapter step's settled effect is still recorded, because it already happened.
"""

import json

from sova.adapters.ed25519_evidence import Ed25519EvidenceVerifier
from sova.domain.change_impact import ImpactAssessment
from sova.domain.contracts import ContractError, EffectRecord, EffectStatus, identifier, utc
from sova.domain.evidence import Gate, VerificationResult, evaluate_gate
from sova.domain.handoff_authority import (
    TERMINAL,
    EffectOutcome,
    HandoffState,
    abort_before_activation,
    complete_operation,
    escalate,
    reconcile,
    require_failback,
    source_restored,
    start,
)
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plans import LifecyclePlan

_LEDGER = {
    EffectStatus.APPLIED: EffectOutcome.EFFECT_CONFIRMED,
    EffectStatus.NOT_APPLIED: EffectOutcome.EFFECT_NOT_APPLIED,
    EffectStatus.UNKNOWN: EffectOutcome.EFFECT_UNKNOWN,
}
_BLOCKED = TERMINAL | {
    HandoffState.SAFE_HOLD,
    HandoffState.ABORTING_PRE_ACTIVATION,
    HandoffState.RESTORING_SOURCE,
}


def _bounded(action):
    return action if len(action) <= 512 else action[:509] + "..."


class SignedObligationEvidence:
    """Latest signed evidence per obligation from registered producer channels."""

    def __init__(self, *, current_envelope, resolve_trust, issuers, clock):
        if not all(callable(f) for f in (current_envelope, resolve_trust, clock)):
            raise ContractError("Evidence lookup, trust, and clock callbacks required")
        if type(issuers) is not dict or not issuers:
            raise ContractError("Registered issuer per verifier version required")
        for version, issuer in issuers.items():
            identifier(version)
            identifier(issuer)
        self.current_envelope, self.clock, self.issuers = current_envelope, clock, issuers
        self.verifier = Ed25519EvidenceVerifier(resolve_trust)

    def __call__(self, obligation):
        issuer = self.issuers.get(obligation.verifier_version)
        if issuer is None:
            return ()
        try:
            envelope = self.current_envelope(
                obligation.scope,
                obligation_id=obligation.obligation_id,
                issuer_id=issuer,
            )
            record = self.verifier.verify(
                envelope, expected_record=envelope.evidence, now=self.clock()
            )
        except Exception:
            # Missing, ambiguous, untrusted, or unreachable: not satisfied.
            return ()
        return (record,)


class HandoffCoordinator:
    def __init__(self, *, store, evidence, changes, clock):
        if not all(callable(f) for f in (evidence, changes, clock)):
            raise ContractError("Evidence source, change impact source, and clock required")
        self.store, self.evidence, self.changes, self.clock = store, evidence, changes, clock

    @staticmethod
    def _check(revision, plan):
        if not isinstance(revision, PlanRevision) or not isinstance(plan, LifecyclePlan):
            raise ContractError("Frozen compiled plan and lowered plan required")
        if plan.origin_plan_digest != revision.digest:
            raise ContractError("Lowered plan does not come from this compiled plan")
        return json.loads(revision.canonical)

    def _current(self, revision, plan):
        doc = self._check(revision, plan)
        record = self.store.current(plan.application_id, doc["unit"]["unit_digest"])
        if record is None or (record.plan_digest, record.compiled_digest) != (
            plan.fingerprint,
            revision.digest,
        ):
            raise ContractError("No authority record for this admitted plan")
        return doc, record

    def impact(self, revision, plan):
        """The current change impact on this admitted plan."""
        doc, record = self._current(revision, plan)
        return self._impact(revision, plan, doc, record)

    def _impact(self, revision, plan, doc, record):
        issued = min(record.next_operation + 1, len(doc["operations"]))
        impact = self.changes(revision, plan, issued=issued)
        if not isinstance(impact, ImpactAssessment) or impact.plan_digest != plan.fingerprint:
            raise ContractError("Change impact for another plan")
        return impact

    def satisfied(self, plan, required_ids, impact):
        return frozenset(o for o, result in self.verdicts(plan, required_ids, impact).items()
                         if result == VerificationResult.PASS)

    def verdicts(self, plan, required_ids, impact):
        """Current verdict per obligation; ambiguous evidence decides nothing."""
        if not isinstance(impact, ImpactAssessment):
            raise ContractError("Change impact assessment required")
        required = set(required_ids)
        obligations = tuple(o for o in plan.obligations if o.obligation_id in required)
        if {o.obligation_id for o in obligations} != required:
            raise ContractError("Operation requires an obligation the plan lacks")
        # Evidence predating a change that affects it no longer counts (INV-08).
        evidence = tuple(item for o in obligations for item in self.evidence(o)
                         if impact.evidence_counts(o.obligation_id, item.observed_at))
        ids = [e.evidence_id for e in evidence]
        if len(ids) != len(set(ids)):
            return {o.obligation_id: VerificationResult.UNKNOWN for o in obligations}
        decision = evaluate_gate(
            Gate.POSTFLIGHT, obligations, evidence, plan.revisions, now=utc(self.clock())
        )
        return {d.obligation.obligation_id: d.result for d in decision.decisions}

    def open(self, revision, plan, *, actor_id):
        self._check(revision, plan)
        return self.store.open(start(revision, plan_digest=plan.fingerprint),
                               actor_id=actor_id)

    def apply_effect(self, revision, plan, *, operation_id, outcome, actor_id):
        if not isinstance(outcome, EffectOutcome):
            raise ContractError("Typed effect outcome required")
        doc, record = self._current(revision, plan)
        ops = doc["operations"]
        if record.next_operation >= len(ops):
            raise ContractError("No planned operation remains")
        op = ops[record.next_operation]
        satisfied, action = frozenset(), "effect." + str(outcome).lower()
        if outcome == EffectOutcome.EFFECT_CONFIRMED:
            impact = self._impact(revision, plan, doc, record)
            if op["adapter"] is None:
                # A platform decision on a stale or unknown plan is never taken.
                impact.require_commands_allowed()
            verdicts = self.verdicts(plan, op["required_evidence"], impact)
            failed = sorted(o for o, r in verdicts.items() if r == VerificationResult.FAIL)
            if failed:
                # The effect happened but its verification failed: a contract
                # breach (§19.1), never a silent "not yet satisfied".
                outcome = EffectOutcome.CONTRACT_BREACH
                action = _bounded("verification_failed:" + ",".join(failed))
            satisfied = frozenset(o for o, r in verdicts.items()
                                  if r == VerificationResult.PASS)
        after = complete_operation(record, revision, operation_id=operation_id,
                                   outcome=outcome, satisfied_evidence=satisfied)
        if after == record:
            return record
        return self.store.transition(record, after, action=action, actor_id=actor_id)

    def supervise(self, revision, plan, *, actor_id):
        """Escalate outcomes that do not come from the effect ledger (§19.1).

        An active security hold is a SECURITY_VIOLATION. A FAIL verdict now
        current for an obligation of an already completed step is a
        CONTRACT_BREACH (a regression after it passed). Returns the record.
        """
        doc, record = self._current(revision, plan)
        if record.state in TERMINAL:
            return record
        if self.store.active_hold(plan.application_id):
            outcome, action = EffectOutcome.SECURITY_VIOLATION, "security_hold_observed"
        else:
            done = [i for op in doc["operations"][:record.next_operation]
                    for i in op["required_evidence"]]
            if not done:
                return record
            impact = self._impact(revision, plan, doc, record)
            failed = sorted(o for o, r in self.verdicts(plan, done, impact).items()
                            if r == VerificationResult.FAIL)
            if not failed:
                return record
            outcome = EffectOutcome.CONTRACT_BREACH
            action = _bounded("verification_regressed:" + ",".join(failed))
        after = escalate(record, revision, outcome=outcome)
        if after == record:
            return record
        return self.store.transition(record, after, action=action, actor_id=actor_id)

    def reconcile(self, revision, plan, *, outcome, actor_id):
        doc, record = self._current(revision, plan)
        required = doc["operations"][record.next_operation]["required_evidence"]
        satisfied = frozenset()
        if outcome == EffectOutcome.EFFECT_CONFIRMED:
            satisfied = self.satisfied(
                plan, required, self._impact(revision, plan, doc, record))
        after = reconcile(record, revision, outcome=outcome, satisfied_evidence=satisfied)
        if after == record:
            return record
        return self.store.transition(record, after, action="reconciled", actor_id=actor_id)

    def abort(self, revision, plan, *, target_inactive_verified, actor_id):
        _, record = self._current(revision, plan)
        after = abort_before_activation(
            record, target_inactive_verified=target_inactive_verified
        )
        return self.store.transition(record, after, action="abort", actor_id=actor_id)

    def source_restored(self, revision, plan, *, source_writes_verified, actor_id):
        _, record = self._current(revision, plan)
        after = source_restored(record, source_writes_verified=source_writes_verified)
        return self.store.transition(record, after, action="source_restore",
                                     actor_id=actor_id)

    def require_failback(self, revision, plan, *, reason, actor_id):
        _, record = self._current(revision, plan)
        after = require_failback(record, reason=reason)
        return self.store.transition(record, after, action="failback_required",
                                     actor_id=actor_id)

    def next_command(self, revision, plan, *, operation_id, epoch):
        """Issue the existing EffectCommand for the next adapter step only."""
        doc, record = self._current(revision, plan)
        if record.state in _BLOCKED:
            raise ContractError("Handoff state does not allow a new command")
        op = doc["operations"][record.next_operation]
        if op["adapter"] is None:
            raise ContractError("Platform-owned step has no adapter command")
        self.store.require_no_hold(plan.application_id)
        self._impact(revision, plan, doc, record).require_commands_allowed()
        return plan.command(op["operation_id"], operation_id=operation_id, epoch=epoch)

    def advance_from_ledger(self, revision, plan, *, effect, actor_id):
        """Advance the next step from its effect-ledger record (None for platform steps)."""
        doc, record = self._current(revision, plan)
        if record.next_operation >= len(doc["operations"]):
            raise ContractError("No planned operation remains")
        op = doc["operations"][record.next_operation]
        if op["adapter"] is None:
            if effect is not None:
                raise ContractError("Platform-owned steps have no ledger effect")
            outcome = EffectOutcome.EFFECT_CONFIRMED
        else:
            if not isinstance(effect, EffectRecord):
                raise ContractError("Effect-ledger record required for an adapter step")
            command = effect.command
            if (
                command.plan_digest != plan.fingerprint
                or command.step_id != op["operation_id"]
                or command.action != op["kind"]
                or command.adapter_id != op["adapter"]["adapter_id"]
            ):
                raise ContractError("Ledger effect belongs to another plan step")
            if effect.status not in _LEDGER:
                raise ContractError("Effect is not settled in the ledger")
            if effect.status == EffectStatus.APPLIED and (
                effect.observation is None
                or effect.observation.command_digest != command.fingerprint
            ):
                raise ContractError("Applied effect lacks its readback observation")
            outcome = _LEDGER[effect.status]
        return self.apply_effect(revision, plan, operation_id=op["operation_id"],
                                 outcome=outcome, actor_id=actor_id)
