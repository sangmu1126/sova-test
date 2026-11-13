"""Effect-ledger outcomes and next-step command issuance, with in-memory stores."""

from datetime import timedelta
import json

from change_support import memory_changes
from contract_world import build_world
import pytest

from sova.application.handoff_coordinator import HandoffCoordinator
from sova.domain.contracts import (
    ContractError,
    EffectObservation,
    EffectRecord,
    EffectStatus,
)
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.handoff_authority import HandoffState
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.held = set()

    def current(self, app, unit):
        return self.rows.get((app, unit))

    def open(self, record, *, actor_id):
        self.rows[(record.application_id, record.unit_digest)] = record
        return record

    def require_no_hold(self, application_id):
        if application_id in self.held:
            raise ContractError("Active security hold blocks handoff progress")

    def transition(self, before, after, *, action, actor_id):
        key = (after.application_id, after.unit_digest)
        assert self.rows[key] == before
        self.rows[key] = after
        return after


@pytest.fixture
def setup(now):
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
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})

    def evidence(obligation):
        return (
            EvidenceRecord(
                "ev-" + obligation.obligation_id, obligation.obligation_id,
                obligation.scope, plan.revisions, obligation.verifier_version,
                now - timedelta(seconds=5), now + timedelta(minutes=5),
                VerificationResult.PASS, "observation", "a" * 64,
            ),
        )

    feed, changes = memory_changes(lambda: now)
    coordinator = HandoffCoordinator(store=MemoryStore(), evidence=evidence,
                                     changes=changes, clock=lambda: now)
    coordinator.open(revision, plan, actor_id="coordinator")
    ops = json.loads(revision.canonical)["operations"]
    return dict(revision=revision, plan=plan, coordinator=coordinator, ops=ops, now=now,
                feed=feed)


def ledger(setup, op, status, *, plan=None, observed=True):
    plan = plan or setup["plan"]
    command = plan.command(op["operation_id"], operation_id="exec-1", epoch=1)
    observation = None
    if status in (EffectStatus.APPLIED, EffectStatus.UNKNOWN) and observed:
        observation = EffectObservation(command.scope, command.fingerprint, status,
                                        setup["now"], "{}")
    return EffectRecord(command, status, 3, observation)


def advance(setup, effect):
    return setup["coordinator"].advance_from_ledger(
        setup["revision"], setup["plan"], effect=effect, actor_id="coordinator")


def test_next_command_is_the_existing_command_for_the_next_adapter_step(setup):
    command = setup["coordinator"].next_command(
        setup["revision"], setup["plan"], operation_id="exec-1", epoch=4)
    first = setup["ops"][0]
    assert (command.step_id, command.action) == (first["operation_id"], "stage_target")
    assert command.plan_digest == setup["plan"].fingerprint
    assert command.fencing_epoch == 4


def test_applied_ledger_effect_with_readback_advances(setup):
    record = advance(setup, ledger(setup, setup["ops"][0], EffectStatus.APPLIED))
    assert record.state == HandoffState.TARGET_PREPARED and record.next_operation == 1


@pytest.mark.parametrize("status", [EffectStatus.PREPARED, EffectStatus.IN_FLIGHT])
def test_unsettled_effects_never_advance(setup, status):
    with pytest.raises(ContractError, match="not settled"):
        advance(setup, ledger(setup, setup["ops"][0], status))


def test_unknown_holds_and_not_applied_changes_nothing(setup):
    first = setup["ops"][0]
    before = setup["coordinator"].next_command(setup["revision"], setup["plan"],
                                               operation_id="exec-1", epoch=1)
    unchanged = advance(setup, ledger(setup, first, EffectStatus.NOT_APPLIED))
    assert unchanged.next_operation == 0 and unchanged.revision == 1
    held = advance(setup, ledger(setup, first, EffectStatus.UNKNOWN))
    assert held.state == HandoffState.SAFE_HOLD
    with pytest.raises(ContractError, match="does not allow"):
        setup["coordinator"].next_command(setup["revision"], setup["plan"],
                                          operation_id="exec-2", epoch=1)
    assert before.step_id == first["operation_id"]


def test_effects_for_another_step_or_plan_are_refused(setup):
    second = setup["ops"][1]
    with pytest.raises(ContractError, match="another plan step"):
        advance(setup, ledger(setup, second, EffectStatus.APPLIED))
    kinds = {o["kind"] for o in json.loads(setup["revision"].canonical)["obligations"]}
    # Same compiled plan lowered with other verifiers: a different approved plan.
    other_plan = lower_plan(setup["revision"], created_by="someone-else",
                            verifiers={k: "v-2" for k in kinds})
    assert other_plan.fingerprint != setup["plan"].fingerprint
    with pytest.raises(ContractError, match="another plan step"):
        advance(setup, ledger(setup, setup["ops"][0], EffectStatus.APPLIED, plan=other_plan))
    with pytest.raises(ContractError, match="ledger record required"):
        advance(setup, None)


def test_platform_steps_take_no_ledger_effect_and_no_adapter_command(setup):
    for op in setup["ops"]:
        if op["adapter"] is None:
            break
        advance(setup, ledger(setup, op, EffectStatus.APPLIED))
    assert op["adapter"] is None
    with pytest.raises(ContractError, match="no adapter command"):
        setup["coordinator"].next_command(setup["revision"], setup["plan"],
                                          operation_id="exec-9", epoch=1)
    with pytest.raises(ContractError, match="no ledger effect"):
        advance(setup, ledger(setup, setup["ops"][0], EffectStatus.APPLIED))
    record = advance(setup, None)
    assert record.state == HandoffState(op["phase"])


def test_no_command_is_issued_while_a_security_hold_is_active(setup):
    setup["coordinator"].store.held.add(setup["plan"].application_id)
    with pytest.raises(ContractError, match="security hold"):
        setup["coordinator"].next_command(setup["revision"], setup["plan"],
                                          operation_id="exec-1", epoch=1)
