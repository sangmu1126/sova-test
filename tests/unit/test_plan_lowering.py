"""C9 lowering and C12 recheck over locally signed synthetic inputs."""

from dataclasses import replace
from datetime import timedelta

from contract_world import build_world
import pytest

from sova.domain.contracts import ContractError
from sova.domain.evidence import Gate
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import (
    PLATFORM_ADAPTER,
    lower_plan,
    verify_approved_plan,
)
from sova.domain.plans import PlanApproval, PlanPurpose

VERIFIERS = {
    "placement_evidence": "placement-verifier-1",
    "independent_write_denial_probe": "fence-probe-1",
    "external_result_lookup": "effect-lookup-1",
    "final_content_comparison": "data-compare-1",
    "business_invariant": "business-oracle-1",
    "target_control_test": "control-test-1",
    "authority_readback": "authority-readback-1",
    "stale_route_probe": "route-probe-1",
    "measured_budget": "budget-meter-1",
    "restore_drill": "restore-drill-1",
}


@pytest.fixture
def world(now):
    world = build_world(now)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    return world


def recompile(world, **changes):
    inputs = dict(
        requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id,
        source=world["source"],
        target=world["target"],
        manifest=world["manifest"],
        qualification=world["qualification"],
        rows=2000,
        authenticate=world["auth"],
        clock=lambda: world["clock"][0],
    )
    return compile_revision(world["contract"], world["snapshot"], **(inputs | changes))


def verifiers_for(revision):
    import json

    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    return {k: VERIFIERS[k] for k in kinds}


def lowered(world):
    revision, result = recompile(world)
    assert result["status"] == "COMPILED"
    return revision, lower_plan(
        revision, created_by="planner", verifiers=verifiers_for(revision)
    )


def test_lowering_is_deterministic_and_traceable_to_the_compiled_plan(world):
    revision, plan = lowered(world)
    again = lower_plan(revision, created_by="planner", verifiers=verifiers_for(revision))
    assert plan == again and plan.fingerprint == again.fingerprint
    assert plan.origin_plan_digest == revision.digest
    assert plan.purpose == PlanPurpose.MIGRATION_PHASE
    assert plan.valid_until == world["qualification"].expires_at
    ids = [s.step_id for s in plan.steps]
    assert all(s.depends_on == (prev,) for prev, s in zip(ids, plan.steps[1:]))
    by_action = {s.action: s for s in plan.steps}
    assert by_action["record_activation_intent"].adapter_id == PLATFORM_ADAPTER
    assert by_action["fence_source_writes"].adapter_id == world["manifest"].adapter_id
    assert by_action["fence_source_writes"].scope.environment_id == "aws-source"
    assert by_action["activate_target"].scope.environment_id == "onprem-target"
    unit = {s.scope.resource_id for s in plan.steps}
    assert len(unit) == 1 and next(iter(unit)).startswith("unit-")


def test_obligations_keep_named_verifiers_and_existing_gates(world):
    _, plan = lowered(world)
    gates = {o.obligation_id: o.due_gate for o in plan.obligations}
    assert gates["fence.source_writes_denied"] == Gate.PREFLIGHT
    assert gates["data.orders"] == Gate.DATA
    assert gates["security.tested_restore"] == Gate.PROTECTION
    assert gates["residency.business_data"] == Gate.REQUIREMENTS
    assert gates["availability.write_admission_pause"] == Gate.POSTFLIGHT
    verifier = {o.obligation_id: o.verifier_version for o in plan.obligations}
    assert verifier["data.orders"] == "data-compare-1"
    assert all(o.required for o in plan.obligations)


def test_missing_or_extra_verifier_names_are_refused(world):
    revision, _ = recompile(world)
    names = verifiers_for(revision)
    names.pop("restore_drill")
    with pytest.raises(ContractError, match="named verifier"):
        lower_plan(revision, created_by="planner", verifiers=names)
    with pytest.raises(ContractError, match="named verifier"):
        lower_plan(
            revision,
            created_by="planner",
            verifiers=verifiers_for(revision) | {"unused": "x"},
        )
    with pytest.raises(ContractError):
        lower_plan({"canonical": "{}"}, created_by="planner", verifiers={})


def test_existing_command_path_binds_the_lowered_plan_digest(world):
    _, plan = lowered(world)
    fence = next(s for s in plan.steps if s.action == "fence_source_writes")
    command = plan.command(fence.step_id, operation_id="op-1", epoch=7)
    assert command.plan_digest == plan.fingerprint
    assert command.action == "fence_source_writes"
    assert command.expected_revision == world["snapshot"].fingerprint


def approve(plan, world, approver="security-approver", minutes=3):
    return PlanApproval(
        plan.fingerprint,
        approver,
        PlanPurpose.MIGRATION_PHASE,
        world["now"] + timedelta(minutes=minutes),
    )


def check(world, plan, approval, **changes):
    revision, _ = recompile(world)
    return verify_approved_plan(
        plan,
        approval,
        recompile=lambda: recompile(world, **changes),
        verifiers=verifiers_for(revision),
        now=world["now"],
    )


def test_current_inputs_and_independent_approval_pass_the_recheck(world):
    _, plan = lowered(world)
    assert check(world, plan, approve(plan, world)) == plan


def test_author_cannot_approve_and_expired_approval_is_refused(world):
    _, plan = lowered(world)
    with pytest.raises(ContractError, match="author"):
        check(world, plan, approve(plan, world, approver="planner"))
    stale = PlanApproval(
        plan.fingerprint, "security-approver", PlanPurpose.MIGRATION_PHASE,
        world["now"] - timedelta(seconds=1),
    )
    with pytest.raises(ContractError, match="expired"):
        check(world, plan, stale)


def test_changed_tampered_or_unauthenticated_inputs_refuse_an_approved_plan(world):
    _, plan = lowered(world)
    approval = approve(plan, world)
    # AT-10: an edited approved plan no longer matches the compiled one.
    tampered = replace(plan, steps=plan.steps[:-1])
    with pytest.raises(ContractError, match="differs"):
        check(world, tampered, approve(tampered, world))
    # AT-11: a recreated resource (new UID) changes the snapshot digest.
    uids = dict(world["snapshot"].resource_uids)
    uids["database"] = "recreated-with-the-same-name"
    recreated = replace(world["snapshot"], resource_uids=tuple(uids.items()))
    with pytest.raises(ContractError, match="no longer compilable"):
        verify_approved_plan(
            plan, approval,
            recompile=lambda: compile_revision(
                world["contract"], recreated,
                requested_nodes=("api-a", "api-b", "database"),
                expected_tenant=world["contract"].tenant_id,
                source=world["source"], target=world["target"],
                manifest=world["manifest"], qualification=world["qualification"],
                rows=2000, authenticate=world["auth"], clock=lambda: world["now"],
            ),
            verifiers=verifiers_for(recompile(world)[0]),
            now=world["now"],
        )
    # AT-13: revoked verifier trust makes the inputs unauthenticated again.
    key = (world["trust"].issuer_id, world["trust"].key_id)
    world["trusts"][key] = replace(world["trust"], revoked=True)
    with pytest.raises(ContractError, match="no longer compilable"):
        verify_approved_plan(
            plan, approval, recompile=lambda: recompile(world),
            verifiers=dict(VERIFIERS), now=world["now"],
        )


def test_recompile_result_must_match_its_frozen_plan(world):
    revision, result = recompile(world)
    _, plan = lowered(world)
    forged = dict(result, plan_digest="0" * 64)
    with pytest.raises(ContractError, match="does not match"):
        verify_approved_plan(
            plan, approve(plan, world), recompile=lambda: (revision, forged),
            verifiers=verifiers_for(revision), now=world["now"],
        )
