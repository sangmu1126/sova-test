"""Authority witness chain and recovery incarnation (AT-33, §15.6)."""

from dataclasses import replace
import json

from contract_world import build_world
import pytest

from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.domain.contracts import ContractError
from sova.domain.handoff_authority import (
    ROLLBACK_RECOVERY,
    EffectOutcome,
    HandoffState,
    complete_operation,
    escalate,
    reconcile,
    recover_incarnation,
    start,
)
from sova.domain.plan_compiler import compile_revision

UNIT = "1" * 64


def test_the_witness_chain_detects_edited_history(tmp_path):
    path = tmp_path / "witness.jsonl"
    witness = FileAuthorityWitness(path, tenant_id="tenant-a")
    assert witness.latest("orders", UNIT) is None
    for revision in (1, 2, 3):
        witness.append("orders", UNIT, f"step-{revision}", {"revision": revision})
    assert witness.latest("orders", UNIT) == {"revision": 3}
    lines = path.read_text().splitlines()
    edited = json.loads(lines[1])
    edited["record"]["revision"] = 99
    lines[1] = json.dumps(edited, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ContractError, match="chain is broken"):
        witness.latest("orders", UNIT)
    with pytest.raises(ContractError, match="chain is broken"):
        FileAuthorityWitness(path, tenant_id="tenant-b").latest("orders", UNIT)


@pytest.fixture
def revision(now):
    world = build_world(now)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, _ = compile_revision(
        world["contract"], world["snapshot"], requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    return revision


def walk(revision, until):
    ops = json.loads(revision.canonical)["operations"]
    records = [start(revision, plan_digest="f" * 64)]
    for op in ops:
        if op["kind"] == until:
            break
        records.append(complete_operation(
            records[-1], revision, operation_id=op["operation_id"],
            outcome=EffectOutcome.EFFECT_CONFIRMED,
            satisfied_evidence=frozenset(op["required_evidence"])))
    return records, ops


def test_recovery_keeps_the_witnessed_decision_and_holds_the_open_step(revision):
    records, ops = walk(revision, "commit_route")
    stored, witnessed = records[2], records[-1]  # the store went back several steps
    recovered = recover_incarnation(stored, witnessed, revision)
    assert recovered.state == HandoffState.SAFE_HOLD and recovered.reason == ROLLBACK_RECOVERY
    assert recovered.held_from == witnessed.state
    assert recovered.activation_possible and recovered.owner_environment == "onprem-target"
    assert recovered.epoch == witnessed.epoch + 1
    assert recovered.revision == witnessed.revision + 1
    # Resuming needs a fresh observation of the held step, as for any hold.
    route = next(o for o in ops if o["kind"] == "commit_route")
    resumed = reconcile(recovered, revision, outcome=EffectOutcome.EFFECT_CONFIRMED,
                        satisfied_evidence=frozenset(route["required_evidence"]))
    assert resumed.state == HandoffState.ROUTE_CONFIRMED
    assert reconcile(recovered, revision, outcome=EffectOutcome.EFFECT_NOT_APPLIED,
                     satisfied_evidence=frozenset()).state == witnessed.state


def test_recovery_never_downgrades_a_security_hold_or_reopens_a_terminal(revision):
    records, _ = walk(revision, "fence_source_writes")
    held = escalate(records[-1], revision, outcome=EffectOutcome.SECURITY_VIOLATION)
    recovered = recover_incarnation(records[1], held, revision)
    assert recovered.reason == "SECURITY_VIOLATION" and recovered.epoch == held.epoch + 1
    done, _ = walk(revision, None)
    final = recover_incarnation(done[3], done[-1], revision)
    assert final.state == HandoffState.HANDOFF_COMPLETE
    assert recover_incarnation(None, done[-1], revision).state == HandoffState.HANDOFF_COMPLETE
    with pytest.raises(ContractError, match="No rollback"):
        recover_incarnation(done[-1], done[-1], revision)
    with pytest.raises(ContractError, match="different units"):
        recover_incarnation(replace(done[3], unit_digest="2" * 64), done[-1], revision)
