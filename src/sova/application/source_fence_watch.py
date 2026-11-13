"""Keep the source fenced after activation, until it is retired (§15.4-15.6, AT-32).

Once activation may have happened, the source must not accept business writes
again: not when the handoff completes, and not when someone restores the source
database from an earlier snapshot whose grants let the old writer back in.
The platform cannot prevent such a restore, but it must notice it and stop the
unit. ``check`` decides the latest signed write-denial observation for the
source with the existing gate rules:

* PASS - the source is still fenced;
* FAIL - the old writer is back: a security incident is opened (deduplicated
  per scope and reason) through the existing hold store, which blocks
  admission, commands and transitions for the service (Part IV);
* anything else - UNKNOWN, reported, never treated as fenced.

Collecting fresh write-denial observations on a schedule is the caller's job;
the watch never infers fencing from the absence of evidence.
"""

import json

from sova.domain.contracts import ContractError, identifier, utc
from sova.domain.evidence import Gate, VerificationResult, evaluate_gate
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plans import LifecyclePlan

FENCE_OBLIGATION = "fence.source_writes_denied"
REACTIVATED = "SOURCE_WRITER_REACTIVATED"


class SourceFenceWatch:
    def __init__(self, *, store, evidence, holds, clock):
        if not callable(evidence) or not callable(clock):
            raise ContractError("Evidence source and trusted clock required")
        if not callable(getattr(holds, "open_incident", None)):
            raise ContractError("Security hold store required")
        self.store, self.evidence, self.holds, self.clock = store, evidence, holds, clock

    def check(self, revision, plan, *, actor_id):
        identifier(actor_id, "actor_id")
        if not isinstance(revision, PlanRevision) or not isinstance(plan, LifecyclePlan):
            raise ContractError("Frozen compiled plan and lowered plan required")
        doc = json.loads(revision.canonical)
        record = self.store.current(plan.application_id, doc["unit"]["unit_digest"])
        if record is None or record.compiled_digest != revision.digest:
            raise ContractError("No authority record for this plan")
        if not record.activation_possible:
            return {"status": "NOT_APPLICABLE", "reason": "activation not possible yet"}
        fence = next((o for o in plan.obligations if o.obligation_id == FENCE_OBLIGATION),
                     None)
        if fence is None:
            raise ContractError("Plan has no source fence obligation")
        evidence = tuple(self.evidence(fence))
        decision = evaluate_gate(Gate.POSTFLIGHT, (fence,), evidence, plan.revisions,
                                 now=utc(self.clock())).decisions[0]
        if decision.result == VerificationResult.PASS:
            return {"status": "FENCED", "evidence_id": decision.evidence_id}
        if decision.result == VerificationResult.FAIL:
            stored, opened = self.holds.open_incident(fence.scope, REACTIVATED, actor_id)
            return {"status": REACTIVATED, "evidence_id": decision.evidence_id,
                    "hold_id": stored.hold.hold_id, "newly_opened": opened}
        return {"status": "UNKNOWN", "reason": decision.reason}
