"""Security and verification outcomes persisted on real PostgreSQL (§19.1, Part IV).

Synthetic inputs and local keys; no live environment is changed.
"""

from datetime import UTC, datetime

from handoff_support import admitted_handoff
import pytest

from sova.adapters.postgres_governance import PostgresSecurityHolds
from sova.domain.contracts import ContractError, ResourceScope
from sova.domain.evidence import VerificationResult
from sova.domain.governance import SecurityHold
from sova.domain.handoff_authority import EffectOutcome, HandoffState
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
        "incident-commander", "Suspected credential compromise", datetime.now(UTC)))
    held = h["coordinator"].supervise(h["revision"], h["plan"], actor_id="supervisor")
    assert held.state == HandoffState.SAFE_HOLD and held.reason == "SECURITY_VIOLATION"
    assert h["store"].current(h["app"], held.unit_digest) == held
    assert h["store"].history(h["app"], held.unit_digest)[-1][1] == "security_hold_observed"
    # Supervising again changes nothing; abort cannot reopen the source during the hold.
    assert h["coordinator"].supervise(h["revision"], h["plan"], actor_id="supervisor") == held
    with pytest.raises(RepositoryConflict, match="security hold"):
        h["coordinator"].abort(h["revision"], h["plan"], target_inactive_verified=True,
                               actor_id="coordinator")
