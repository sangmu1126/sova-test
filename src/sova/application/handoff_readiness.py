"""One readiness view of a unit's handoff (MIG-SPEC §24.2 readiness, §27.4).

Execution, change impact, security, independence, and recovery are reported
as separate states, never folded into one "ready" flag. Commands are allowed
only when every independent condition allows them, and each blocking reason
is listed. Recovery actions are derived from the authority model's own
transition guards. Retiring the source is offered for review only when the
independence assessment allows it; it is always a separate approval.
"""

import json

from sova.domain.contracts import ContractError
from sova.domain.handoff_authority import TERMINAL, HandoffState, available_recoveries
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plans import LifecyclePlan

_HELD = {HandoffState.SAFE_HOLD, HandoffState.ABORTING_PRE_ACTIVATION,
         HandoffState.RESTORING_SOURCE}
# While a security hold is active the store accepts only transitions into
# SAFE_HOLD or FAILBACK_REQUIRED (Part IV); other recoveries wait for release.
_ALLOWED_UNDER_HOLD = {"REQUIRE_FAILBACK"}


class HandoffReadiness:
    def __init__(self, *, store, claims, coordinator, exit_readiness):
        self.store, self.claims = store, claims
        self.coordinator, self.exit_readiness = coordinator, exit_readiness

    def assess(self, revision, plan):
        if not isinstance(revision, PlanRevision) or not isinstance(plan, LifecyclePlan):
            raise ContractError("Frozen compiled plan and lowered plan required")
        doc = json.loads(revision.canonical)
        record = self.store.current(plan.application_id, doc["unit"]["unit_digest"])
        if record is None or (record.plan_digest, record.compiled_digest) != (
            plan.fingerprint, revision.digest
        ):
            raise ContractError("No authority record for this admitted plan")
        ops = doc["operations"]
        impact = self.coordinator.impact(revision, plan)
        hold = self.store.active_hold(plan.application_id)
        claimed = [key for _, digest_, key, _ in
                   self.claims.active(application_id=plan.application_id)
                   if digest_ == plan.fingerprint]
        blocked = []
        if record.state in TERMINAL:
            blocked.append("HANDOFF_" + str(record.state))
        elif record.state in _HELD:
            blocked.append("AUTHORITY_" + str(record.state))
        if hold:
            blocked.append("SECURITY_HOLD_ACTIVE")
        if impact.plan_stale:
            blocked.append("PLAN_STALE")
        if impact.plan_unknown:
            blocked.append("CHANGE_FEED_INCOMPLETE")
        if not claimed and record.state not in TERMINAL:
            blocked.append("CLAIMS_RELEASED")
        nxt = ops[record.next_operation] if record.next_operation < len(ops) else None
        independence = self.exit_readiness.assess(revision, plan)
        recoveries = list(available_recoveries(record, revision))
        if independence["source_retirement"]["reviewable"]:
            recoveries.append("SOURCE_RETIREMENT_REVIEW")
        deferred = [r for r in recoveries if hold and r not in _ALLOWED_UNDER_HOLD]
        recoveries = [r for r in recoveries if r not in deferred]
        return {
            "schema_version": 1,
            "application_id": plan.application_id,
            "plan_digest": plan.fingerprint,
            "compiled_digest": revision.digest,
            "failback_of": (doc.get("failback_of") or {}).get("forward_compiled_digest"),
            "execution": {
                "state": str(record.state),
                "owner_environment": record.owner_environment,
                "epoch": record.epoch,
                "activation_possible": record.activation_possible,
                "next_step": None if nxt is None else nxt["kind"],
                "next_step_is_platform": None if nxt is None else nxt["adapter"] is None,
                "hold_reason": record.reason if record.state == HandoffState.SAFE_HOLD
                else None,
            },
            "commands": {"allowed": not blocked, "blocked_by": blocked},
            "change_impact": impact.document,
            "security": {"hold_active": hold},
            "claims": {"active": len(claimed)},
            "independence": {
                "levels": {k: v["status"] for k, v in independence["levels"].items()},
                "identity": independence["identity"]["status"],
                "residual_dependencies": independence["residual_dependencies"],
            },
            "recovery": {
                "available": sorted(recoveries),
                "after_security_hold_release": sorted(deferred),
                "source_retirement": independence["source_retirement"]["approval"],
            },
        }
