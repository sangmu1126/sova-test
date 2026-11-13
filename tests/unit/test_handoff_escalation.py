"""Outcomes from verification and security paths, not the effect ledger (§19.1)."""

from datetime import timedelta
import json

from change_support import memory_changes
from contract_world import build_world
import pytest

from sova.application.handoff_coordinator import HandoffCoordinator
from sova.domain.contracts import ContractError
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan


class Store:
    def __init__(self):
        self.rows, self.held, self.actions = {}, False, []

    def current(self, app, unit):
        return self.rows.get((app, unit))

    def open(self, record, *, actor_id):
        self.rows[(record.application_id, record.unit_digest)] = record
        return record

    def active_hold(self, application_id):
        return self.held

    def require_no_hold(self, application_id):
        if self.held:
            raise ContractError("Active security hold blocks handoff progress")

    def transition(self, before, after, *, action, actor_id):
        self.actions.append(action)
        self.rows[(after.application_id, after.unit_digest)] = after
        return after


@pytest.fixture
def h(now):
    world = build_world(now)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, result = compile_revision(
        world["contract"], world["snapshot"], requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    clock = {"now": now + timedelta(minutes=1)}
    verdict = {}  # obligation id -> result of its latest evidence (default PASS)

    def evidence(o):
        result = verdict.get(o.obligation_id, VerificationResult.PASS)
        if result is None:
            return ()
        at = clock["now"] - timedelta(seconds=1)
        return (EvidenceRecord(f"ev-{o.obligation_id}-{result}", o.obligation_id, o.scope,
                               plan.revisions, o.verifier_version, at,
                               at + timedelta(minutes=10), result, "observation",
                               "a" * 64),)

    store = Store()
    _, changes = memory_changes(lambda: clock["now"])
    c = HandoffCoordinator(store=store, evidence=evidence, changes=changes,
                           clock=lambda: clock["now"])
    c.open(revision, plan, actor_id="coordinator")
    ops = json.loads(revision.canonical)["operations"]
    return dict(c=c, revision=revision, plan=plan, store=store, verdict=verdict, ops=ops)


def run_until(h, kind):
    record = None
    for op in h["ops"]:
        if op["kind"] == kind:
            return op, record
        record = h["c"].apply_effect(h["revision"], h["plan"], operation_id=op["operation_id"],
                                     outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
    raise AssertionError(kind)


def confirm(h, op):
    return h["c"].apply_effect(h["revision"], h["plan"], operation_id=op["operation_id"],
                               outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")


def test_failed_fence_verification_is_a_contract_breach_hold(h):
    fence, _ = run_until(h, "fence_source_writes")
    h["verdict"]["fence.source_writes_denied"] = VerificationResult.FAIL
    held = confirm(h, fence)
    assert held.state == HandoffState.SAFE_HOLD and held.reason == "CONTRACT_BREACH"
    assert not held.activation_possible and not held.fence_verified
    assert h["store"].actions[-1] == "verification_failed:fence.source_writes_denied"
    # Not auto-reconciled even once the probe passes; abort is still possible.
    h["verdict"].clear()
    with pytest.raises(ContractError, match="separate recovery decision"):
        h["c"].reconcile(h["revision"], h["plan"], outcome=EffectOutcome.EFFECT_CONFIRMED,
                         actor_id="c")
    aborting = h["c"].abort(h["revision"], h["plan"], target_inactive_verified=True,
                            actor_id="c")
    assert aborting.state == HandoffState.ABORTING_PRE_ACTIVATION


def test_failed_route_probe_after_activation_requires_failback(h):
    route, _ = run_until(h, "commit_route")
    h["verdict"]["route.stale_writes_rejected"] = VerificationResult.FAIL
    record = confirm(h, route)
    assert record.state == HandoffState.FAILBACK_REQUIRED
    assert record.activation_possible and record.reason == "CONTRACT_BREACH"


def test_missing_evidence_is_still_only_unsatisfied(h):
    fence, _ = run_until(h, "fence_source_writes")
    h["verdict"]["fence.source_writes_denied"] = None
    with pytest.raises(ContractError, match="evidence is not satisfied"):
        confirm(h, fence)
    h["verdict"]["fence.source_writes_denied"] = VerificationResult.UNKNOWN
    with pytest.raises(ContractError, match="evidence is not satisfied"):
        confirm(h, fence)


def test_supervision_catches_a_regression_of_a_completed_step(h):
    _, record = run_until(h, "copy_content")  # staging and fence already passed
    assert h["c"].supervise(h["revision"], h["plan"], actor_id="s") == record
    # A missing or unknown observation is not a breach.
    h["verdict"].update({"residency.audit": None,
                         "residency.backups": VerificationResult.UNKNOWN})
    assert h["c"].supervise(h["revision"], h["plan"], actor_id="s") == record
    h["verdict"]["residency.business_data"] = VerificationResult.FAIL
    held = h["c"].supervise(h["revision"], h["plan"], actor_id="s")
    assert held.state == HandoffState.SAFE_HOLD and held.reason == "CONTRACT_BREACH"
    assert h["store"].actions[-1] == "verification_regressed:residency.business_data"


def test_security_hold_is_a_security_violation_and_outranks_other_holds(h):
    fence, _ = run_until(h, "fence_source_writes")
    unknown = h["c"].apply_effect(h["revision"], h["plan"], operation_id=fence["operation_id"],
                                  outcome=EffectOutcome.EFFECT_UNKNOWN, actor_id="c")
    assert unknown.reason == "EFFECT_UNKNOWN"
    h["store"].held = True
    held = h["c"].supervise(h["revision"], h["plan"], actor_id="s")
    assert held.state == HandoffState.SAFE_HOLD and held.reason == "SECURITY_VIOLATION"
    assert held.held_from == unknown.held_from  # still the state it held
    assert h["store"].actions[-1] == "security_hold_observed"
    h["store"].held = False
    with pytest.raises(ContractError, match="separate recovery decision"):
        h["c"].reconcile(h["revision"], h["plan"], outcome=EffectOutcome.EFFECT_CONFIRMED,
                         actor_id="c")


def test_terminal_handoffs_are_not_supervised(h):
    for op in h["ops"]:
        record = confirm(h, op)
    assert record.state == HandoffState.HANDOFF_COMPLETE
    h["store"].held = True
    h["verdict"]["route.stale_writes_rejected"] = VerificationResult.FAIL
    assert h["c"].supervise(h["revision"], h["plan"], actor_id="s") == record
