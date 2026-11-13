"""Per-unit business-authority handoff state (MIG-SPEC-001 §15). Pure, no I/O.

``AuthorityRecord`` is the platform's view of which environment may commit
business changes for one migration unit, distinct from the execution lease.
Transitions follow the frozen plan's operation order and only advance on a
confirmed effect whose required evidence is all satisfied. Uncertain or partial
effects hold (SAFE_HOLD) instead of guessing. Once an activation intent is
recorded, activation must be assumed possible: the source is never reopened
automatically and only a separate failback plan can move authority back.

Callers persist records with compare-and-set on ``revision`` and must take
effect outcomes from authenticated observations, never from a client claim.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
import json

from sova.domain.contracts import ContractError, digest, identifier
from sova.domain.plan_compiler import PlanRevision


class HandoffState(StrEnum):
    SOURCE_ACTIVE = "SOURCE_ACTIVE"
    TARGET_PREPARED = "TARGET_PREPARED"
    SYNCING = "SYNCING"
    CUTOVER_READY = "CUTOVER_READY"
    QUIESCING = "QUIESCING"
    SOURCE_FENCED = "SOURCE_FENCED"
    FINAL_STATE_VERIFIED = "FINAL_STATE_VERIFIED"
    TARGET_ACTIVATION_INTENT = "TARGET_ACTIVATION_INTENT"
    TARGET_ACTIVE_OBSERVED = "TARGET_ACTIVE_OBSERVED"
    ROUTE_CONFIRMED = "ROUTE_CONFIRMED"
    STABILIZING = "STABILIZING"
    HANDOFF_COMPLETE = "HANDOFF_COMPLETE"
    SAFE_HOLD = "SAFE_HOLD"
    ABORTING_PRE_ACTIVATION = "ABORTING_PRE_ACTIVATION"
    RESTORING_SOURCE = "RESTORING_SOURCE"
    FAILBACK_REQUIRED = "FAILBACK_REQUIRED"
    ABORTED = "ABORTED"


class EffectOutcome(StrEnum):
    """§19.1 per-operation failure classification from authenticated observation."""

    EFFECT_CONFIRMED = "EFFECT_CONFIRMED"
    EFFECT_NOT_APPLIED = "EFFECT_NOT_APPLIED"
    EFFECT_UNKNOWN = "EFFECT_UNKNOWN"
    PARTIAL_EFFECT = "PARTIAL_EFFECT"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    CONTRACT_BREACH = "CONTRACT_BREACH"


TERMINAL = {
    HandoffState.HANDOFF_COMPLETE,
    HandoffState.FAILBACK_REQUIRED,
    HandoffState.ABORTED,
}


@dataclass(frozen=True, slots=True)
class AuthorityRecord:
    tenant_id: str
    application_id: str
    unit_digest: str
    plan_digest: str
    compiled_digest: str
    source_environment: str
    target_environment: str
    revision: int
    state: HandoffState
    owner_environment: str
    epoch: int
    next_operation: int
    activation_possible: bool
    fence_verified: bool
    held_from: HandoffState | None = None
    hold_operation: str | None = None
    reason: str | None = None

    def __post_init__(self):
        for field in ("tenant_id", "application_id", "source_environment",
                      "target_environment", "owner_environment"):
            identifier(getattr(self, field), field)
        for field in ("unit_digest", "plan_digest", "compiled_digest"):
            digest(getattr(self, field))
        if not isinstance(self.state, HandoffState):
            raise ContractError("Typed handoff state required")
        for field in ("revision", "epoch", "next_operation"):
            value = getattr(self, field)
            if type(value) is not int or value < (0 if field == "next_operation" else 1):
                raise ContractError("Positive revision/epoch and operation index required")
        for field in ("activation_possible", "fence_verified"):
            if type(getattr(self, field)) is not bool:
                raise ContractError("Exact authority booleans required")
        if self.owner_environment not in (self.source_environment, self.target_environment):
            raise ContractError("Authority owner must be the source or the target")
        if (self.state == HandoffState.SAFE_HOLD) != (self.held_from is not None):
            raise ContractError("Only SAFE_HOLD records the state it held")
        # Structural safety: authority cannot sit on the target without a
        # recorded intent, and a possible activation cannot coexist with a
        # source-side restore.
        if self.owner_environment == self.target_environment and not self.activation_possible:
            raise ContractError("Target authority requires a recorded activation intent")
        if self.activation_possible and self.state in (
            HandoffState.ABORTING_PRE_ACTIVATION,
            HandoffState.RESTORING_SOURCE,
            HandoffState.ABORTED,
        ):
            raise ContractError("Possible target activation forbids source restore")
        if self.activation_possible and not self.fence_verified:
            raise ContractError("Activation intent requires a verified source fence")

    @property
    def document(self):
        return {
            "tenant_id": self.tenant_id,
            "application_id": self.application_id,
            "unit_digest": self.unit_digest,
            "plan_digest": self.plan_digest,
            "compiled_digest": self.compiled_digest,
            "source_environment": self.source_environment,
            "target_environment": self.target_environment,
            "revision": self.revision,
            "state": str(self.state),
            "owner_environment": self.owner_environment,
            "epoch": self.epoch,
            "next_operation": self.next_operation,
            "activation_possible": self.activation_possible,
            "fence_verified": self.fence_verified,
            "held_from": None if self.held_from is None else str(self.held_from),
            "hold_operation": self.hold_operation,
            "reason": self.reason,
        }


def _plan(revision):
    if not isinstance(revision, PlanRevision):
        raise ContractError("Frozen compiler plan required")
    return json.loads(revision.canonical)


def start(revision, *, plan_digest):
    """Open the authority record for an admitted plan, with the source active."""
    plan = _plan(revision)
    digest(plan_digest)
    source = plan["environments"]["source"]
    return AuthorityRecord(
        plan["tenant_id"], plan["service_id"], plan["unit"]["unit_digest"], plan_digest,
        revision.digest, source, plan["environments"]["target"], 1,
        HandoffState.SOURCE_ACTIVE, source, 1, 0, False, False,
    )


def _next(record, **changes):
    return replace(record, revision=record.revision + 1, **changes)


def _bind(record, revision):
    plan = _plan(revision)
    if (revision.digest, plan["unit"]["unit_digest"]) != (
        record.compiled_digest,
        record.unit_digest,
    ):
        raise ContractError("Authority record belongs to another plan")
    return plan["operations"]


def _advance(record, op, last):
    changes = {"state": HandoffState(op["phase"]), "next_operation": record.next_operation + 1,
               "held_from": None, "hold_operation": None, "reason": None}
    if op["kind"] == "fence_source_writes":
        changes["fence_verified"] = True
    if op["kind"] == "record_activation_intent":
        if not record.fence_verified:
            raise ContractError("Activation intent requires a verified source fence")
        # From here the target may already be active; a new epoch fences stale
        # commands of the previous one.
        changes.update(activation_possible=True, epoch=record.epoch + 1)
    if op["kind"] == "activate_target":
        changes["owner_environment"] = record.target_environment
    if last:
        changes["state"] = HandoffState.HANDOFF_COMPLETE
    return _next(record, **changes)


def complete_operation(record, revision, *, operation_id, outcome, satisfied_evidence):
    """Apply one authenticated effect observation for the next planned operation."""
    if not isinstance(record, AuthorityRecord) or not isinstance(outcome, EffectOutcome):
        raise ContractError("Typed authority record and outcome required")
    if type(satisfied_evidence) is not frozenset:
        raise ContractError("Immutable satisfied evidence set required")
    operations = _bind(record, revision)
    if record.state in TERMINAL:
        raise ContractError("Handoff already reached a terminal state")
    if record.state in (HandoffState.ABORTING_PRE_ACTIVATION, HandoffState.RESTORING_SOURCE):
        raise ContractError("Handoff is aborting; no planned operation may continue")
    if record.state == HandoffState.SAFE_HOLD:
        raise ContractError("SAFE_HOLD requires reconciliation before continuing")
    if record.next_operation >= len(operations):
        raise ContractError("No planned operation remains")
    op = operations[record.next_operation]
    if operation_id != op["operation_id"]:
        raise ContractError("Operations must complete in plan order")
    if outcome == EffectOutcome.EFFECT_CONFIRMED:
        missing = set(op["required_evidence"]) - satisfied_evidence
        if missing:
            raise ContractError("Required evidence is not satisfied")
        return _advance(record, op, record.next_operation == len(operations) - 1)
    if outcome == EffectOutcome.EFFECT_NOT_APPLIED:
        return record  # nothing happened; an approved retry may follow
    if outcome == EffectOutcome.CONTRACT_BREACH and record.activation_possible:
        return _next(record, state=HandoffState.FAILBACK_REQUIRED, reason=str(outcome))
    return _next(
        record,
        state=HandoffState.SAFE_HOLD,
        held_from=record.state,
        hold_operation=op["operation_id"],
        reason=str(outcome),
    )


def reconcile(record, revision, *, outcome, satisfied_evidence):
    """Resolve a SAFE_HOLD from a fresh authenticated observation of the held operation."""
    if not isinstance(record, AuthorityRecord) or record.state != HandoffState.SAFE_HOLD:
        raise ContractError("Only a held handoff can be reconciled")
    if not isinstance(outcome, EffectOutcome) or type(satisfied_evidence) is not frozenset:
        raise ContractError("Typed outcome and immutable evidence required")
    operations = _bind(record, revision)
    op = operations[record.next_operation]
    if record.reason in (
        str(EffectOutcome.SECURITY_VIOLATION),
        str(EffectOutcome.CONTRACT_BREACH),
    ):
        raise ContractError("Security or contract holds need a separate recovery decision")
    if outcome == EffectOutcome.EFFECT_CONFIRMED:
        if set(op["required_evidence"]) - satisfied_evidence:
            raise ContractError("Required evidence is not satisfied")
        resumed = replace(record, state=record.held_from, held_from=None,
                          hold_operation=None)
        return _advance(resumed, op, record.next_operation == len(operations) - 1)
    if outcome == EffectOutcome.EFFECT_NOT_APPLIED:
        return _next(record, state=record.held_from, held_from=None,
                     hold_operation=None, reason=None)
    return record  # still unknown: stay held, never guess


def abort_before_activation(record, *, target_inactive_verified):
    """Start a pre-activation abort; refused once activation may have happened."""
    if not isinstance(record, AuthorityRecord):
        raise ContractError("Typed authority record required")
    if record.activation_possible:
        raise ContractError("Activation may have happened; a failback plan is required")
    if record.state in TERMINAL:
        raise ContractError("Handoff already reached a terminal state")
    if target_inactive_verified is not True:
        raise ContractError("Verified target inactivity required before abort")
    return _next(record, state=HandoffState.ABORTING_PRE_ACTIVATION,
                 held_from=None, hold_operation=None, reason="ABORT_REQUESTED")


def source_restored(record, *, source_writes_verified):
    """Finish a pre-activation abort after the source accepts writes again."""
    if not isinstance(record, AuthorityRecord):
        raise ContractError("Typed authority record required")
    if record.state == HandoffState.ABORTING_PRE_ACTIVATION:
        return _next(record, state=HandoffState.RESTORING_SOURCE)
    if record.state != HandoffState.RESTORING_SOURCE or source_writes_verified is not True:
        raise ContractError("Source restore requires an abort and verified source writes")
    return _next(record, state=HandoffState.ABORTED, reason="ABORTED_BEFORE_ACTIVATION")


def require_failback(record, *, reason):
    """After a possible activation the only way back is a separate failback plan."""
    if not isinstance(record, AuthorityRecord) or not record.activation_possible:
        raise ContractError("Failback applies only after an activation intent")
    if record.state in TERMINAL:
        raise ContractError("Handoff already reached a terminal state")
    identifier(reason, "failback reason", limit=1024)
    return _next(record, state=HandoffState.FAILBACK_REQUIRED, held_from=None,
                 hold_operation=None, reason=reason)
