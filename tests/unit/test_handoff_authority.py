"""§15 authority transitions and an exhaustive bounded model check (§28.3).

The model explores every reachable record of one compiled plan under all
event kinds; it checks the plan-level state machine, not adapters or I/O.
"""

from collections import deque
from dataclasses import replace
import json

from contract_world import build_world
import pytest

from sova.domain.contracts import ContractError
from sova.domain.handoff_authority import (
    TERMINAL,
    AuthorityRecord,
    EffectOutcome,
    HandoffState,
    abort_before_activation,
    escalate,
    complete_operation,
    reconcile,
    require_failback,
    source_restored,
    start,
)
from sova.domain.plan_compiler import compile_revision

PLAN_DIGEST = "f" * 64


@pytest.fixture(scope="module")
def revision():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    world = build_world(now)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, result = compile_revision(
        world["contract"], world["snapshot"],
        requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    assert result["status"] == "COMPILED"
    return revision


def ops(revision):
    return json.loads(revision.canonical)["operations"]


def evidence_for(op):
    return frozenset(op["required_evidence"])


def run_all(revision):
    record = start(revision, plan_digest=PLAN_DIGEST)
    for op in ops(revision):
        record = complete_operation(
            record, revision, operation_id=op["operation_id"],
            outcome=EffectOutcome.EFFECT_CONFIRMED, satisfied_evidence=evidence_for(op),
        )
    return record


def test_confirmed_operations_with_evidence_complete_the_handoff(revision):
    record = run_all(revision)
    assert record.state == HandoffState.HANDOFF_COMPLETE
    assert record.owner_environment == record.target_environment
    assert record.activation_possible and record.fence_verified
    assert record.epoch == 2
    assert record.next_operation == len(ops(revision))


def test_order_and_evidence_are_enforced(revision):
    record = start(revision, plan_digest=PLAN_DIGEST)
    first, second = ops(revision)[:2]
    with pytest.raises(ContractError, match="plan order"):
        complete_operation(record, revision, operation_id=second["operation_id"],
                           outcome=EffectOutcome.EFFECT_CONFIRMED,
                           satisfied_evidence=evidence_for(second))
    assert first["required_evidence"]  # residency must be proven before staging
    with pytest.raises(ContractError, match="evidence"):
        complete_operation(record, revision, operation_id=first["operation_id"],
                           outcome=EffectOutcome.EFFECT_CONFIRMED,
                           satisfied_evidence=frozenset())


@pytest.mark.parametrize("outcome", [EffectOutcome.EFFECT_UNKNOWN, EffectOutcome.PARTIAL_EFFECT])
def test_uncertainty_holds_and_blocks_the_next_step_until_reconciled(revision, outcome):
    record = start(revision, plan_digest=PLAN_DIGEST)
    first, second = ops(revision)[:2]
    held = complete_operation(record, revision, operation_id=first["operation_id"],
                              outcome=outcome, satisfied_evidence=evidence_for(first))
    assert held.state == HandoffState.SAFE_HOLD and held.held_from == record.state
    with pytest.raises(ContractError, match="SAFE_HOLD"):
        complete_operation(held, revision, operation_id=second["operation_id"],
                           outcome=EffectOutcome.EFFECT_CONFIRMED,
                           satisfied_evidence=evidence_for(second))
    assert reconcile(held, revision, outcome=EffectOutcome.EFFECT_UNKNOWN,
                     satisfied_evidence=frozenset()) == held
    resumed = reconcile(held, revision, outcome=EffectOutcome.EFFECT_CONFIRMED,
                        satisfied_evidence=evidence_for(first))
    assert resumed.state == HandoffState(first["phase"])
    assert resumed.next_operation == 1


def test_activation_intent_forbids_abort_and_source_reopen(revision):
    record = start(revision, plan_digest=PLAN_DIGEST)
    for op in ops(revision):
        record = complete_operation(record, revision, operation_id=op["operation_id"],
                                    outcome=EffectOutcome.EFFECT_CONFIRMED,
                                    satisfied_evidence=evidence_for(op))
        if op["kind"] == "record_activation_intent":
            break
    assert record.activation_possible and record.epoch == 2
    assert record.owner_environment == record.source_environment
    with pytest.raises(ContractError, match="failback"):
        abort_before_activation(record, target_inactive_verified=True)
    with pytest.raises(ContractError):
        replace(record, state=HandoffState.RESTORING_SOURCE)
    failback = require_failback(record, reason="post-activation contract breach")
    assert failback.state == HandoffState.FAILBACK_REQUIRED


def test_pre_activation_abort_needs_verified_target_inactivity(revision):
    record = start(revision, plan_digest=PLAN_DIGEST)
    with pytest.raises(ContractError, match="inactivity"):
        abort_before_activation(record, target_inactive_verified=False)
    aborting = abort_before_activation(record, target_inactive_verified=True)
    restoring = source_restored(aborting, source_writes_verified=False)
    assert restoring.state == HandoffState.RESTORING_SOURCE
    with pytest.raises(ContractError):
        source_restored(restoring, source_writes_verified=False)
    assert source_restored(restoring, source_writes_verified=True).state == HandoffState.ABORTED


def test_security_and_contract_holds_are_not_auto_reconciled(revision):
    record = start(revision, plan_digest=PLAN_DIGEST)
    first = ops(revision)[0]
    held = complete_operation(record, revision, operation_id=first["operation_id"],
                              outcome=EffectOutcome.SECURITY_VIOLATION,
                              satisfied_evidence=evidence_for(first))
    with pytest.raises(ContractError, match="separate recovery"):
        reconcile(held, revision, outcome=EffectOutcome.EFFECT_CONFIRMED,
                  satisfied_evidence=evidence_for(first))


def test_record_cannot_be_bound_to_another_plan_or_forged(revision):
    record = start(revision, plan_digest=PLAN_DIGEST)
    with pytest.raises(ContractError):
        replace(record, owner_environment=record.target_environment)
    with pytest.raises(ContractError):
        replace(record, activation_possible=True)  # no verified fence
    other = replace(record, compiled_digest="e" * 64)
    with pytest.raises(ContractError, match="another plan"):
        complete_operation(other, revision, operation_id=ops(revision)[0]["operation_id"],
                           outcome=EffectOutcome.EFFECT_CONFIRMED,
                           satisfied_evidence=evidence_for(ops(revision)[0]))


# ---------------------------------------------------------------------------
# Exhaustive bounded model check over every reachable record.


def key(record):
    data = record.document
    data.pop("revision")
    return json.dumps(data, sort_keys=True)


def events(revision, record):
    plan = ops(revision)
    out = []
    op = plan[record.next_operation] if record.next_operation < len(plan) else None
    if op is not None:
        for outcome in EffectOutcome:
            for evidence in (evidence_for(op), frozenset()):
                out.append(("complete", outcome, evidence, lambda r, o=outcome, e=evidence:
                            complete_operation(r, revision, operation_id=op["operation_id"],
                                               outcome=o, satisfied_evidence=e)))
        for outcome in (EffectOutcome.EFFECT_CONFIRMED, EffectOutcome.EFFECT_NOT_APPLIED,
                        EffectOutcome.EFFECT_UNKNOWN):
            for evidence in (evidence_for(op), frozenset()):
                out.append(("reconcile", outcome, evidence, lambda r, o=outcome, e=evidence:
                            reconcile(r, revision, outcome=o, satisfied_evidence=e)))
    for verified in (True, False):
        out.append(("abort", verified, None,
                    lambda r, v=verified: abort_before_activation(r, target_inactive_verified=v)))
        out.append(("restored", verified, None,
                    lambda r, v=verified: source_restored(r, source_writes_verified=v)))
    out.append(("failback", None, None, lambda r: require_failback(r, reason="operator")))
    for outcome in (EffectOutcome.SECURITY_VIOLATION, EffectOutcome.CONTRACT_BREACH):
        out.append(("escalate", outcome, None,
                    lambda r, o=outcome: escalate(r, revision, outcome=o)))
    return out


def test_bounded_model_check_safety_and_progress(revision):
    plan = ops(revision)
    fence_index = next(i for i, o in enumerate(plan) if o["kind"] == "fence_source_writes")
    initial = start(revision, plan_digest=PLAN_DIGEST)
    seen = {key(initial): initial}
    edges = {key(initial): set()}
    queue = deque([initial])
    while queue:
        record = queue.popleft()
        for name, a, b, fire in events(revision, record):
            try:
                after = fire(record)
            except ContractError:
                continue
            assert isinstance(after, AuthorityRecord)
            # Revisions and epochs never move backwards.
            assert after.revision >= record.revision and after.epoch >= record.epoch
            # at_most_one_effective_authority: the target owns only after intent.
            if after.owner_environment == after.target_environment:
                assert after.activation_possible
            # no_activation_without_valid_fence (the fence step had its evidence).
            if after.activation_possible:
                assert after.fence_verified and after.next_operation > fence_index
            # no_automatic_source_reopen_after_possible_target_activation.
            if record.activation_possible:
                assert after.activation_possible
                assert after.state not in (HandoffState.ABORTING_PRE_ACTIVATION,
                                           HandoffState.RESTORING_SOURCE,
                                           HandoffState.ABORTED,
                                           HandoffState.SOURCE_ACTIVE)
            # no_conflicting_step_after_unknown: a held record never completes a step.
            if record.state == HandoffState.SAFE_HOLD and name == "complete":
                raise AssertionError("a held record completed an operation")
            # A hold is left for a non-terminal state only by reconciliation or abort.
            if record.state == HandoffState.SAFE_HOLD and after.state not in (
                HandoffState.SAFE_HOLD, HandoffState.FAILBACK_REQUIRED
            ):
                assert name in ("reconcile", "abort")
            # Completion only after every operation was confirmed with evidence.
            if after.state == HandoffState.HANDOFF_COMPLETE:
                assert after.next_operation == len(plan)
                assert name in ("complete", "reconcile") and a == EffectOutcome.EFFECT_CONFIRMED
            # Terminal states are absorbing.
            assert record.state not in TERMINAL or after == record
            k = key(after)
            edges[key(record)].add(k)
            if k not in seen:
                seen[k] = after
                edges[k] = set()
                queue.append(after)
    # Progress: every reachable record can still reach a terminal state.
    terminal = {k for k, r in seen.items() if r.state in TERMINAL}
    reaches = set(terminal)
    changed = True
    while changed:
        changed = False
        for k, targets in edges.items():
            if k not in reaches and targets & reaches:
                reaches.add(k)
                changed = True
    assert reaches == set(seen), "some reachable state cannot terminate"
    states = {r.state for r in seen.values()}
    assert {HandoffState.HANDOFF_COMPLETE, HandoffState.ABORTED,
            HandoffState.FAILBACK_REQUIRED, HandoffState.SAFE_HOLD} <= states
    assert len(seen) > 30
