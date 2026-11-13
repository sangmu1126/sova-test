"""Record a plan's structural reaffirmation for change impact (INV-08).

The feed port adds::

    record_reaffirmation(*, application_id, plan_digest, compiled_digest,
                         snapshot_digest, observed_at, actor_id)
    reaffirmed_at(plan_digest) -> datetime | None
"""

from datetime import datetime

from sova.domain.contracts import ContractError, identifier
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plan_reaffirmation import reaffirm_structure
from sova.domain.plans import LifecyclePlan


class PlanReaffirmation:
    def __init__(self, *, feed, clock):
        if not callable(clock) or not callable(getattr(feed, "record_reaffirmation", None)):
            raise ContractError("Reaffirmation store and trusted clock required")
        self.feed, self.clock = feed, clock

    def reaffirm(self, revision, plan, contract, snapshot, *, expected_tenant, authenticate,
                 actor_id):
        if not isinstance(revision, PlanRevision) or not isinstance(plan, LifecyclePlan):
            raise ContractError("Frozen compiled plan and lowered plan required")
        if plan.origin_plan_digest != revision.digest:
            raise ContractError("Lowered plan does not come from this compiled plan")
        identifier(actor_id, "actor_id")
        result = reaffirm_structure(revision, contract, snapshot,
                                    expected_tenant=expected_tenant,
                                    authenticate=authenticate, clock=self.clock)
        if result["status"] == "REAFFIRMED":
            self.feed.record_reaffirmation(
                application_id=plan.application_id, plan_digest=plan.fingerprint,
                compiled_digest=revision.digest, snapshot_digest=result["snapshot_digest"],
                observed_at=datetime.fromisoformat(result["observed_at"]), actor_id=actor_id)
        return result
