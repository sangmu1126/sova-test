"""Assess a unit's independence from signed drill evidence (§20, AT-35/36).

Drill evidence is read the same way as handoff evidence: the latest signed
observation on the registered producer channel, decided with the existing
``evaluate_gate`` binding rules (obligation, scope, the handoff plan's
revisions, verifier version, freshness). Evidence observed before a change
that affects the drill does not count (INV-08). Running a drill - blocking a
source dependency, revoking platform access - is itself a scoped, approved
operation outside this service; this service only judges its results.
"""

import json

from sova.domain.contracts import ContractError, identifier, utc
from sova.domain.evidence import (
    Gate,
    VerificationObligation,
    VerificationResult,
    evaluate_gate,
)
from sova.domain.exit_readiness import EXIT_DRILL_KIND, DrillVerdict, assess_exit, exit_drills
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plans import LifecyclePlan


class ExitReadiness:
    def __init__(self, *, store, evidence, changes, clock, verifier_version):
        if not all(callable(f) for f in (evidence, changes, clock)):
            raise ContractError("Evidence source, change impact source, and clock required")
        self.store, self.evidence, self.changes, self.clock = store, evidence, changes, clock
        self.verifier_version = identifier(verifier_version, "verifier_version")

    def obligations(self, revision, plan):
        """Drill obligations, scoped like the plan's target-side steps."""
        if not isinstance(revision, PlanRevision) or not isinstance(plan, LifecyclePlan):
            raise ContractError("Frozen compiled plan and lowered plan required")
        if plan.origin_plan_digest != revision.digest:
            raise ContractError("Lowered plan does not come from this compiled plan")
        target = json.loads(revision.canonical)["environments"]["target"]
        scope = next(s.scope for s in plan.steps if s.scope.environment_id == target)
        return tuple(
            VerificationObligation(d.drill_id, scope, Gate.POSTFLIGHT, self.verifier_version)
            for d in exit_drills(revision)
        )

    def assess(self, revision, plan):
        obligations = self.obligations(revision, plan)
        doc = json.loads(revision.canonical)
        record = self.store.current(plan.application_id, doc["unit"]["unit_digest"])
        if record is None or (record.plan_digest, record.compiled_digest) != (
            plan.fingerprint, revision.digest
        ):
            raise ContractError("No authority record for this admitted plan")
        impact = self.changes(
            revision, plan, issued=len(doc["operations"]),
            extra_obligations=tuple((o.obligation_id, EXIT_DRILL_KIND, o.verifier_version)
                                    for o in obligations),
        )
        if impact.plan_digest != plan.fingerprint:
            raise ContractError("Change impact for another plan")
        evidence, invalidated = [], set()
        for o in obligations:
            found = tuple(self.evidence(o))
            counted = tuple(e for e in found
                            if impact.evidence_counts(o.obligation_id, e.observed_at))
            if found and not counted:
                invalidated.add(o.obligation_id)
            evidence.extend(counted)
        ids = [e.evidence_id for e in evidence]
        if len(ids) != len(set(ids)):
            raise ContractError("Ambiguous drill evidence")
        decision = evaluate_gate(Gate.POSTFLIGHT, obligations, tuple(evidence),
                                 plan.revisions, now=utc(self.clock()))
        verdicts = {}
        for d in decision.decisions:
            oid, reason = d.obligation.obligation_id, d.reason
            if oid in impact.unknown_obligations:
                reason = "change_feed_incomplete"
            elif oid in invalidated:
                reason = "invalidated_by_change"
            result = d.result
            if result == VerificationResult.NOT_APPLICABLE:
                # A waiver never proves independence.
                result, reason = VerificationResult.UNKNOWN, "waiver_not_accepted"
            verdicts[oid] = DrillVerdict(result, d.evidence_id, reason)
        assessment = assess_exit(revision, record, verdicts)
        assessment["change_impact"] = {"as_of": impact.as_of.isoformat(),
                                       "reasons": list(impact.reasons)}
        return assessment
