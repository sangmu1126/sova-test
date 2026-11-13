"""Clean failback as a separate plan for a forward handoff (§15.4, §19.3)."""

from datetime import timedelta
import json

from change_support import memory_changes
from contract_world import build_world
import pytest

from sova.application.handoff_coordinator import HandoffCoordinator
from sova.domain.contracts import ContractError
from sova.domain.evidence import EvidenceRecord, Gate, VerificationResult
from sova.domain.handoff_authority import EffectOutcome, HandoffState, start
from sova.domain.plan_compiler import FAILBACK_STRATEGY, compile_failback, compile_revision
from sova.domain.plan_conflicts import WRITE, lock_set
from sova.domain.plan_lowering import lower_plan

NODES = ("api-a", "api-b", "database")


class Store:
    def __init__(self):
        self.rows = {}

    def current(self, app, unit):
        return self.rows.get((app, unit))

    def open(self, record, *, actor_id):
        self.rows[(record.application_id, record.unit_digest)] = record
        return record

    def require_no_hold(self, application_id):
        return None

    def transition(self, before, after, *, action, actor_id):
        self.rows[(after.application_id, after.unit_digest)] = after
        return after


def compiled(world, now, **extra):
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    return dict(contract=world["contract"], snapshot=world["snapshot"],
                requested_nodes=NODES, expected_tenant=world["contract"].tenant_id,
                source=world["source"], target=world["target"], manifest=world["manifest"],
                qualification=world["qualification"], rows=2000, authenticate=world["auth"],
                clock=lambda: now, **extra)


def coordinator(now, plan):
    clock = {"now": now + timedelta(minutes=1)}

    def evidence(o):
        at = clock["now"] - timedelta(seconds=1)
        return (EvidenceRecord(f"ev-{o.obligation_id}", o.obligation_id, o.scope,
                               plan.revisions, o.verifier_version, at,
                               at + timedelta(minutes=10), VerificationResult.PASS,
                               "observation", "a" * 64),)

    _, changes = memory_changes(lambda: clock["now"])
    return HandoffCoordinator(store=Store(), evidence=evidence, changes=changes,
                              clock=lambda: clock["now"])


def run(c, revision, plan, *, until=None):
    record = None
    for op in json.loads(revision.canonical)["operations"]:
        if op["kind"] == until:
            break
        record = c.apply_effect(revision, plan, operation_id=op["operation_id"],
                                outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
    return record


@pytest.fixture
def forward(now):
    world = build_world(now)
    kwargs = compiled(world, now)
    revision, result = compile_revision(**kwargs)
    assert result["status"] == "COMPILED"
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    c = coordinator(now, plan)
    c.open(revision, plan, actor_id="coordinator")
    run(c, revision, plan, until="commit_route")  # target activated, route not confirmed
    record = c.require_failback(revision, plan, reason="contract breach after activation",
                                actor_id="coordinator")
    assert record.state == HandoffState.FAILBACK_REQUIRED and record.activation_possible
    return dict(revision=revision, plan=plan, record=record, world=world)


def failback_inputs(forward, now, **world_changes):
    world = build_world(now, contract=forward["world"]["contract"],
                        strategy=FAILBACK_STRATEGY, **{
                            "reverse": True, "environment": "synthetic-target",
                            "uid_prefix": "target-uid-", **world_changes})
    return compiled(world, now)


def test_failback_compiles_back_to_the_forward_source_resources(forward, now):
    revision, result = compile_failback(forward["revision"], forward["record"],
                                        **failback_inputs(forward, now))
    assert result["status"] == "COMPILED", result["blockers"]
    doc, fwd = json.loads(revision.canonical), json.loads(forward["revision"].canonical)
    assert doc["environments"] == {"source": "onprem-target", "target": "aws-source"}
    back = doc["failback_of"]
    assert back["forward_compiled_digest"] == forward["revision"].digest
    assert back["forward_epoch"] == forward["record"].epoch
    assert back["return_resources"] == fwd["resources"]
    assert back["reason"] == "contract breach after activation"
    kinds = [o["kind"] for o in doc["operations"]]
    assert kinds[0] == "verify_return_target" and "stage_target" not in kinds
    at = {o["operation_id"]: o["kind"] for o in doc["operations"]}
    by_id = {o["id"]: o for o in doc["obligations"]}
    assert at[by_id["failback.return_target_identity"]["required_at"]] == (
        "verify_return_target")
    # New target-side writes come back only as proven by the final comparison (AT-30).
    assert at[by_id["data.orders"]["required_at"]] == "verify_final_state"
    locks = dict(lock_set(revision)[0])
    assert locks[("source", "synthetic-source", "synthetic-uid-database")] == WRITE
    assert locks[("authority", "synthetic-source", "synthetic-uid-database")] == WRITE
    kinds = {o["kind"] for o in doc["obligations"]}
    lowered = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    gates = {o.obligation_id: o.due_gate for o in lowered.obligations}
    assert gates["failback.return_target_identity"] == Gate.PREFLIGHT
    record = start(revision, plan_digest=lowered.fingerprint)
    assert record.epoch == forward["record"].epoch + 1
    assert record.predecessor == forward["revision"].digest
    assert record.owner_environment == "onprem-target"


def test_failback_runs_authority_back_to_the_original_source(forward, now):
    revision, result = compile_failback(forward["revision"], forward["record"],
                                        **failback_inputs(forward, now))
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    c = coordinator(now, plan)
    c.open(revision, plan, actor_id="coordinator")
    record = run(c, revision, plan)
    assert record.state == HandoffState.HANDOFF_COMPLETE
    assert record.owner_environment == "aws-source"
    assert record.epoch == forward["record"].epoch + 2  # failback start + its own intent


def test_failback_preconditions(forward, now):
    inputs = failback_inputs(forward, now)
    with pytest.raises(ContractError, match="FAILBACK_REQUIRED"):
        compile_failback(forward["revision"], forward["record"].__class__(
            **{**forward["record"].document, "state": HandoffState.HANDOFF_COMPLETE,
               "held_from": None}), **inputs)
    # The failback template is never compiled on its own.
    _, alone = compile_revision(**inputs)
    assert alone["status"] == "UNSUPPORTED"
    assert "FAILBACK_REQUIRES_FORWARD_HANDOFF" in {b["code"] for b in alone["blockers"]}
    # Same direction as the forward handoff is not a failback.
    _, same = compile_failback(forward["revision"], forward["record"],
                               **failback_inputs(forward, now, reverse=False))
    assert "FAILBACK_DIRECTION_MISMATCH" in {b["code"] for b in same["blockers"]}
    # A different unit than the one handed off (here: one more member).
    _, wider = compile_failback(forward["revision"], forward["record"],
                                **{**inputs, "requested_nodes": NODES + ("audit",)})
    assert "FAILBACK_UNIT_MISMATCH" in {b["code"] for b in wider["blockers"]}
    assert wider["status"] != "COMPILED" and wider["plan"] is None


def test_a_failback_of_a_failback_is_refused(forward, now):
    revision, _ = compile_failback(forward["revision"], forward["record"],
                                   **failback_inputs(forward, now))
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    c = coordinator(now, plan)
    c.open(revision, plan, actor_id="coordinator")
    run(c, revision, plan, until="commit_route")
    failed = c.require_failback(revision, plan, reason="again", actor_id="coordinator")
    with pytest.raises(ContractError, match="new forward handoff"):
        compile_failback(revision, failed, **failback_inputs(forward, now))
