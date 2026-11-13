"""Apply authenticated observations to a unit's persisted authority record.

Evidence satisfaction is never taken from the caller: for each obligation an
operation requires, the coordinator reads the latest signed observation on the
registered producer channel, verifies its signature and current issuer trust,
and decides it with the existing ``evaluate_gate`` binding rules (obligation,
scope, plan revisions, verifier version, freshness). Handoff steps accept PASS
only; a NOT_APPLICABLE exception does not satisfy a safety step.

The effect outcome passed in must come from the authenticated effect ledger
(``postgres_effects``) for the same operation; wiring that ledger is separate.
"""

import json

from sova.adapters.ed25519_evidence import Ed25519EvidenceVerifier
from sova.domain.contracts import ContractError, identifier, utc
from sova.domain.evidence import Gate, VerificationResult, evaluate_gate
from sova.domain.handoff_authority import (
    EffectOutcome,
    abort_before_activation,
    complete_operation,
    reconcile,
    require_failback,
    source_restored,
    start,
)
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plans import LifecyclePlan


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
    def __init__(self, *, store, evidence, clock):
        if not callable(evidence) or not callable(clock):
            raise ContractError("Evidence source and trusted clock required")
        self.store, self.evidence, self.clock = store, evidence, clock

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

    def satisfied(self, plan, required_ids):
        required = set(required_ids)
        obligations = tuple(o for o in plan.obligations if o.obligation_id in required)
        if {o.obligation_id for o in obligations} != required:
            raise ContractError("Operation requires an obligation the plan lacks")
        evidence = tuple(item for o in obligations for item in self.evidence(o))
        ids = [e.evidence_id for e in evidence]
        if len(ids) != len(set(ids)):
            return frozenset()  # ambiguous evidence never satisfies
        decision = evaluate_gate(
            Gate.POSTFLIGHT, obligations, evidence, plan.revisions, now=utc(self.clock())
        )
        return frozenset(
            d.obligation.obligation_id
            for d in decision.decisions
            if d.result == VerificationResult.PASS
        )

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
        required = ops[record.next_operation]["required_evidence"]
        satisfied = (
            self.satisfied(plan, required)
            if outcome == EffectOutcome.EFFECT_CONFIRMED
            else frozenset()
        )
        after = complete_operation(record, revision, operation_id=operation_id,
                                   outcome=outcome, satisfied_evidence=satisfied)
        if after == record:
            return record
        return self.store.transition(record, after, action="effect." + str(outcome).lower(),
                                     actor_id=actor_id)

    def reconcile(self, revision, plan, *, outcome, actor_id):
        doc, record = self._current(revision, plan)
        required = doc["operations"][record.next_operation]["required_evidence"]
        satisfied = (
            self.satisfied(plan, required)
            if outcome == EffectOutcome.EFFECT_CONFIRMED
            else frozenset()
        )
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
