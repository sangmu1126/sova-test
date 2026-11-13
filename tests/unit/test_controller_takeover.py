"""Existing automation must be verifiably paused before the platform writes (§14.4)."""

from dataclasses import replace
from datetime import timedelta
import json

from change_support import memory_changes
from contract_world import build_world
import pytest

from sova.adapters.registration.dependency_codec import decode_dependency_snapshot
from sova.application.handoff_coordinator import HandoffCoordinator
from sova.application.plan_explanation import explain_plan
from sova.domain.contracts import ContractError
from sova.domain.evidence import EvidenceRecord, Gate, VerificationResult
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan

CONTROLLERS = (("database", "terraform:orders-prod"), ("api-a", "k8s-operator:orders"),
               ("audit", "terraform:observability"))
TF = "controller.terraform:orders-prod.paused"


def compiled(now, controllers=CONTROLLERS):
    world = build_world(now)
    world["snapshot"] = replace(world["snapshot"], controllers=controllers)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, result = compile_revision(
        world["contract"], world["snapshot"], requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    assert result["status"] == "COMPILED", result["blockers"]
    return world, revision


def test_controllers_of_unit_members_are_named_and_must_be_paused_first(now):
    _, revision = compiled(now)
    doc = json.loads(revision.canonical)
    assert doc["controllers"]["write_ownership"] == {
        "k8s-operator:orders": ["api-a"], "terraform:orders-prod": ["database"]}
    assert "terraform:observability" not in json.dumps(doc["controllers"])  # not a member
    at = {o["operation_id"]: o["kind"] for o in doc["operations"]}
    by_id = {o["id"]: o for o in doc["obligations"]}
    assert at[by_id[TF]["required_at"]] == "pause_admission"
    kinds = {o["kind"] for o in doc["obligations"]}
    lowered = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    assert {o.obligation_id: o.due_gate for o in lowered.obligations}[TF] == Gate.PREFLIGHT
    explained = explain_plan(revision)
    assert explained["controllers"]["handback"].startswith("separate_approval_required")


def test_undeclared_controllers_keep_existing_snapshot_digests(now):
    world = build_world(now)
    plain = world["snapshot"]
    assert "controllers" not in plain.document
    declared = replace(plain, controllers=CONTROLLERS)
    assert declared.fingerprint != plain.fingerprint
    assert decode_dependency_snapshot(declared.document) == declared
    assert decode_dependency_snapshot(plain.document) == plain
    with pytest.raises(ContractError):
        decode_dependency_snapshot({**plain.document, "controllers": {}})
    with pytest.raises(ContractError, match="unknown node"):
        replace(plain, controllers=(("ghost", "terraform:x"),))


@pytest.mark.parametrize("verdict,expected", [
    (None, "unsatisfied"), (VerificationResult.FAIL, HandoffState.SAFE_HOLD),
    (VerificationResult.PASS, HandoffState.QUIESCING)])
def test_pause_admission_waits_for_the_controller_to_be_paused(now, verdict, expected):
    _, revision = compiled(now)
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    clock = now + timedelta(minutes=1)

    def evidence(o):
        result = verdict if o.obligation_id == TF else VerificationResult.PASS
        if result is None:
            return ()
        return (EvidenceRecord(f"ev-{o.obligation_id}", o.obligation_id, o.scope,
                               plan.revisions, o.verifier_version, clock - timedelta(seconds=1),
                               clock + timedelta(minutes=5), result, "observation",
                               "a" * 64),)

    class Store:
        rows = {}

        def current(self, app, unit):
            return self.rows.get((app, unit))

        def open(self, record, *, actor_id):
            self.rows[(record.application_id, record.unit_digest)] = record
            return record

        def transition(self, before, after, *, action, actor_id):
            self.rows[(after.application_id, after.unit_digest)] = after
            return after

    _, changes = memory_changes(lambda: clock)
    c = HandoffCoordinator(store=Store(), evidence=evidence, changes=changes,
                           clock=lambda: clock)
    c.open(revision, plan, actor_id="c")
    ops = json.loads(revision.canonical)["operations"]
    c.apply_effect(revision, plan, operation_id=ops[0]["operation_id"],
                   outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
    pause = next(o for o in ops if o["kind"] == "pause_admission")
    assert TF in pause["required_evidence"]
    if expected == "unsatisfied":
        with pytest.raises(ContractError, match="evidence is not satisfied"):
            c.apply_effect(revision, plan, operation_id=pause["operation_id"],
                           outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
        return
    record = c.apply_effect(revision, plan, operation_id=pause["operation_id"],
                            outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
    assert record.state == expected
