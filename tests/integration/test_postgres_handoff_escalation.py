"""Security and verification outcomes persisted on real PostgreSQL (§19.1, Part IV).

Synthetic inputs and local keys; no live environment is changed.
"""

from datetime import UTC, datetime, timedelta

from handoff_support import admitted_handoff
import pytest

from sova.adapters.postgres_governance import PostgresSecurityHolds
from sova.application.handoff_readiness import HandoffReadiness
from sova.domain.contracts import ContractError, ResourceScope
from sova.domain.evidence import VerificationResult
from sova.domain.governance import HoldRelease, SecurityHold
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.domain.plans import PlanApproval, PlanPurpose
from sova.ports.plans import RepositoryConflict

pytestmark = pytest.mark.postgres


@pytest.fixture
def started(postgres_database, now):
    h = admitted_handoff(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    return h


def advance_to(h, kind):
    for op in h["ops"]:
        if op["kind"] == kind:
            return op
        for obligation_id in op["required_evidence"]:
            h["prove"](obligation_id)
        h["coordinator"].apply_effect(h["revision"], h["plan"], operation_id=op["operation_id"],
                                      outcome=EffectOutcome.EFFECT_CONFIRMED,
                                      actor_id="coordinator")
    raise AssertionError(kind)


def test_signed_fence_failure_is_held_as_a_contract_breach(started):
    h = started
    fence = advance_to(h, "fence_source_writes")
    h["prove"]("fence.source_writes_denied", VerificationResult.FAIL)
    held = h["coordinator"].apply_effect(h["revision"], h["plan"],
                                         operation_id=fence["operation_id"],
                                         outcome=EffectOutcome.EFFECT_CONFIRMED,
                                         actor_id="coordinator")
    assert held.state == HandoffState.SAFE_HOLD and held.reason == "CONTRACT_BREACH"
    history = h["store"].history(h["app"], held.unit_digest)
    assert history[-1][1] == "verification_failed:fence.source_writes_denied"
    with pytest.raises(ContractError, match="does not allow"):
        h["coordinator"].next_command(h["revision"], h["plan"], operation_id="x", epoch=1)


def test_security_hold_mid_handoff_is_recorded_as_a_security_violation(started):
    h = started
    advance_to(h, "copy_content")
    PostgresSecurityHolds(h["connect"], tenant_id=h["tenant"]).open_hold(SecurityHold(
        "incident-7", ResourceScope(h["tenant"], h["app"], "aws-source", "db"),
        "incident-commander", "Suspected credential compromise", datetime.now(UTC) - timedelta(seconds=1)))
    held = h["coordinator"].supervise(h["revision"], h["plan"], actor_id="supervisor")
    assert held.state == HandoffState.SAFE_HOLD and held.reason == "SECURITY_VIOLATION"
    assert h["store"].current(h["app"], held.unit_digest) == held
    assert h["store"].history(h["app"], held.unit_digest)[-1][1] == "security_hold_observed"
    # Supervising again changes nothing; abort cannot reopen the source during the hold.
    assert h["coordinator"].supervise(h["revision"], h["plan"], actor_id="supervisor") == held
    with pytest.raises(RepositoryConflict, match="security hold"):
        h["coordinator"].abort(h["revision"], h["plan"], target_inactive_verified=True,
                               actor_id="coordinator")


def test_readiness_reports_the_hold_and_the_remaining_recovery(started):
    h = started
    readiness = HandoffReadiness(store=h["store"], claims=h["claims"],
                                 coordinator=h["coordinator"], exit_readiness=h["readiness"])
    before = readiness.assess(h["revision"], h["plan"])
    assert before["commands"]["allowed"] and before["claims"]["active"] > 0
    advance_to(h, "copy_content")
    PostgresSecurityHolds(h["connect"], tenant_id=h["tenant"]).open_hold(SecurityHold(
        "incident-9", ResourceScope(h["tenant"], h["app"], "aws-source", "db"),
        "incident-commander", "Suspected credential compromise", datetime.now(UTC) - timedelta(seconds=1)))
    h["coordinator"].supervise(h["revision"], h["plan"], actor_id="supervisor")
    after = readiness.assess(h["revision"], h["plan"])
    assert after["commands"]["blocked_by"] == ["AUTHORITY_SAFE_HOLD", "SECURITY_HOLD_ACTIVE"]
    assert after["execution"]["hold_reason"] == "SECURITY_VIOLATION"
    # Abort is allowed by the model but the store refuses it during the hold.
    assert after["recovery"]["available"] == []
    assert after["recovery"]["after_security_hold_release"] == [
        "ABORT_BEFORE_ACTIVATION", "RESUME_AFTER_REVIEW"]


def test_security_incident_hold_release_reapproval_then_resume(started):
    """Part V.3-7: incident -> hold -> release by an independent approver ->
    the plan approved again -> a fresh observation resumes the held step."""
    h = started
    copy = advance_to(h, "copy_content")
    holds = PostgresSecurityHolds(h["connect"], tenant_id=h["tenant"])
    scope = ResourceScope(h["tenant"], h["app"], "aws-source", "db")
    raised = datetime.now(UTC) - timedelta(seconds=1)
    holds.open_hold(SecurityHold("incident-11", scope, "incident-commander",
                                 "Suspected credential compromise", raised))
    held = h["coordinator"].supervise(h["revision"], h["plan"], actor_id="supervisor")
    assert held.reason == "SECURITY_VIOLATION"

    def resume(approval_id):
        return h["coordinator"].resume_after_review(
            h["revision"], h["plan"], lifecycle=h["lifecycle"], approval_id=approval_id,
            outcome=EffectOutcome.EFFECT_NOT_APPLIED, actor_id="coordinator")

    with pytest.raises(RepositoryConflict, match="security hold"):
        resume("approval-1")
    now = datetime.now(UTC)
    holds.release_hold(h["app"], HoldRelease(
        "incident-11", scope, h["plan"].revisions, 1, "security-requester",
        "security-approver", "Credentials rotated and access re-verified", "ev-rotation",
        "e" * 64, now, now + timedelta(minutes=10)))
    with pytest.raises(RepositoryConflict, match="approved again after the hold"):
        resume("approval-1")  # the admission-time approval predates the hold
    h["lifecycle"].save_approval(h["app"], h["plan"].plan_id, "approval-2", PlanApproval(
        h["plan"].fingerprint, "security-approver", PlanPurpose.MIGRATION_PHASE,
        datetime.now(UTC) + timedelta(minutes=3)))
    resumed = resume("approval-2")
    assert resumed.state == held.held_from and resumed.reason is None
    command = h["coordinator"].next_command(h["revision"], h["plan"], operation_id="op",
                                            epoch=1)
    assert command.step_id == copy["operation_id"]
    assert h["store"].history(h["app"], held.unit_digest)[-1][1] == (
        "resumed_after_review:approval-2")
