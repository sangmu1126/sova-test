"""Operator hold semantics: authority is preserved; none of these authorize replay."""
import pytest

from sova.service.recovery_status import recovery_projection


@pytest.mark.parametrize("state,status,mode,live,expected", [
    ("SOURCE_FENCED", "unknown", "mutate", True, "EFFECT_RESULT_UNKNOWN"),
    ("SOURCE_FENCED", "in_flight", "mutate", False, "OWNER_EXPIRED_UNSETTLED"),
    ("SOURCE_FENCED", "in_flight", None, False, "OWNER_EXPIRED_UNSETTLED"),
    ("SOURCE_FENCED", "in_flight", "reconcile", True, "RECOVERY_PENDING"),
    ("SOURCE_FENCED", "prepared", "mutate", True, None),
    ("HANDOFF_COMPLETE", "in_flight", "mutate", True, "TERMINAL_WITH_PENDING_EFFECTS"),
    ("SAFE_HOLD", None, None, False, "AUTHORITY_HOLD"),
    ("FAILBACK_REQUIRED", None, None, False, "AUTHORITY_HOLD"),
    ("HANDOFF_COMPLETE", None, None, False, None),
])
def test_unsettled_effects_are_not_hidden_by_authority_state(state, status, mode, live, expected):
    result = recovery_projection(state=state,
        effects=[{"status": status}] if status else [], mode=mode, lease_live=live)
    assert result["reason"] == expected
    assert result["required"] is (expected is not None)
    assert result["automatic_replay_allowed"] is result["authorizes_execution"] is False


@pytest.mark.parametrize("flag,reason", [("security_hold", "SECURITY_HOLD"),
                                         ("other_pending", "APPLICATION_PENDING_EFFECTS")])
def test_application_barriers_survive_an_empty_current_plan_list(flag, reason):
    result = recovery_projection(state="SOURCE_ACTIVE", effects=[], mode=None,
                                 lease_live=False, **{flag: True})
    assert result["required"] is True and result["reason"] == reason
