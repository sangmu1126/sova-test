"""C0-C11 compilation over locally signed synthetic inputs; no live evidence."""

from dataclasses import replace
from datetime import timedelta
import json

from contract_world import build_world
import pytest

from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError
from sova.domain.plan_compiler import (
    COMPILER_VERSION,
    TEMPLATES,
    compile_plan,
    required_operations,
)
from sova.domain.service_contracts import ServiceContract


def signed(world):
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    return world


@pytest.fixture
def world(now):
    return signed(build_world(now))


def compile_(world, **changes):
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
    snapshot = changes.pop("snapshot", world["snapshot"])
    contract = changes.pop("contract", world["contract"])
    return compile_plan(contract, snapshot, **(inputs | changes))


def codes(result):
    return {b["code"] for b in result["blockers"]}


def test_authenticated_inputs_compile_a_frozen_plan_that_grants_nothing(world):
    result = compile_(world)
    assert result["status"] == "COMPILED", result["blockers"]
    assert result["approval_available"] is True
    assert result["executable"] is result["authorizes_execution"] is False
    plan = result["plan"]
    assert plan["compiler_version"] == COMPILER_VERSION
    assert [o["kind"] for o in plan["operations"]] == [
        s.step_id for s in TEMPLATES["offline-copy"]
    ]
    assert "evaluated_at" not in json.dumps(plan)
    assert plan["valid_until"] == world["qualification"].expires_at.isoformat()


def test_same_frozen_inputs_give_the_same_digest_across_evaluation_time(world):
    first = compile_(world)
    world["clock"][0] = world["now"] + timedelta(seconds=20)
    later = compile_(world, clock=lambda: world["now"] + timedelta(seconds=20))
    assert later["status"] == "COMPILED"
    assert later["evaluated_at"] != first["evaluated_at"]
    assert later["plan_digest"] == first["plan_digest"]
    assert later["plan"] == first["plan"]


def test_handoff_dag_order_sets_and_recovery_boundaries(world):
    ops = compile_(world)["plan"]["operations"]
    kinds = [o["kind"] for o in ops]
    assert kinds.index("fence_source_writes") < kinds.index("copy_content")
    assert kinds.index("verify_final_state") < kinds.index("record_activation_intent")
    assert kinds.index("record_activation_intent") < kinds.index("activate_target")
    for previous, current in zip(ops, ops[1:]):
        assert current["depends_on"] == [previous["operation_id"]]
    boundary = {o["kind"]: o["recovery_boundary"] for o in ops}
    assert boundary["verify_final_state"] == "pre_activation"
    assert boundary["record_activation_intent"] == "post_activation_intent"
    assert boundary["commit_route"] == "post_activation_intent"
    assert all(o["on_uncertain"] == "observe_or_safe_hold" for o in ops)
    fence = next(o for o in ops if o["kind"] == "fence_source_writes")
    assert fence["write_set"] == ["source/database/write-policy"]
    intent = next(o for o in ops if o["kind"] == "record_activation_intent")
    assert intent["adapter"] is None  # platform-owned, never an adapter command
    activate = next(o for o in ops if o["kind"] == "activate_target")
    assert "source_fence_verified_and_latched" in activate["requires"]
    assert "authority/unit" in activate["write_set"]


def test_contract_requirements_become_gate_obligations_not_assumptions(world):
    plan = compile_(world)["plan"]
    by_id = {o["id"]: o for o in plan["obligations"]}
    at = {o["operation_id"]: o["kind"] for o in plan["operations"]}
    spec = world["contract"].document["spec"]
    for control in spec["security"]["required_controls"]:
        assert at[by_id[f"security.{control}"]["required_at"]] == (
            "record_activation_intent"
        )
    for name in spec["data"]["protected_sets"]:
        assert at[by_id[f"data.{name}"]["required_at"]] == "verify_final_state"
    assert at[by_id["residency.business_data"]["required_at"]] == "stage_target"
    assert "residency.ai_inputs" not in by_id  # empty category: nothing to place
    assert by_id["availability.write_admission_pause"]["budget_ms"] == 5000
    assert at[by_id["fence.source_writes_denied"]["required_at"]] == (
        "fence_source_writes"
    )
    assert "operations.restore_test" in by_id
    assert plan["recovery"]["source_auto_reopen_after_possible_activation"] == (
        "forbidden"
    )
    assert plan["recovery"]["source_retirement"] == "separate_approval_required"
    for op in plan["operations"]:
        expected = sorted(i for i, o in by_id.items() if o["required_at"] == op["operation_id"])
        assert op["required_evidence"] == expected


def test_strategy_templates_change_the_plan_and_its_digest(now):
    offline = compile_(signed(build_world(now)))
    cdc_world = signed(build_world(now, strategy="snapshot-cdc"))
    cdc = compile_(cdc_world)
    assert cdc["status"] == "COMPILED"
    assert cdc["plan_digest"] != offline["plan_digest"]
    kinds = [o["kind"] for o in cdc["plan"]["operations"]]
    assert kinds.index("start_sync") < kinds.index("pause_admission")
    assert kinds.index("fence_source_writes") < kinds.index("apply_final_changes")


def test_missing_operations_are_explained_not_planned(now):
    world = signed(build_world(now, operations=("copy_content", "fence_writes")))
    result = compile_(world)
    assert result["status"] == "UNSUPPORTED"
    assert result["plan"] is None and result["approval_available"] is False
    missing = next(
        b for b in result["blockers"] if b["code"] == "CAPABILITY_OPERATION_UNSUPPORTED"
    )
    assert missing["missing_operations"] == sorted(
        set(required_operations("offline-copy")) - {"copy_content", "fence_writes"}
    )


def test_unknown_strategy_and_uncompiled_profiles_are_unsupported(now, world):
    unknown = signed(build_world(now, strategy="bespoke-script"))
    assert compile_(unknown)["status"] == "UNSUPPORTED"
    assert "STRATEGY_TEMPLATE_UNKNOWN" in codes(compile_(unknown))
    data = example_service_contract().document
    data["spec"]["migration"]["profile"] = "PARTITIONED_HANDOFF"
    other = signed(build_world(now, contract=ServiceContract.from_document(data)))
    result = compile_(other)
    assert result["status"] == "UNSUPPORTED"
    assert "PROFILE_NOT_COMPILED" in codes(result)


def test_swapped_direction_is_not_inferred_from_a_forward_qualification(world):
    result = compile_(world, source=world["target"], target=world["source"])
    assert result["status"] == "NEEDS_EVIDENCE"
    assert "QUALIFICATION_BINDING_MISMATCH" in codes(result)
    assert result["plan"] is None


def test_partial_unit_needs_remediation_with_the_members_to_add(world):
    result = compile_(world, requested_nodes=("api-a",))
    assert result["status"] == "REQUIRES_REMEDIATION"
    merge = next(b for b in result["blockers"] if b["code"] == "BOUNDARY_MERGE_REQUIRED")
    assert merge["additional_members"] == ["api-b", "database"]


def test_unregistered_inputs_hold_as_needs_evidence(now):
    world = build_world(now)
    result = compile_(world)
    assert result["status"] == "NEEDS_EVIDENCE"
    assert {
        "CONTRACT_REGISTRATION_REQUIRED",
        "SNAPSHOT_AUTHENTICATION_REQUIRED",
        "QUALIFICATION_AUTHENTICATION_FAILED",
    } <= codes(result)
    world["sign"](world["snapshot"])
    world["sign"](world["qualification"])
    only_contract = compile_(world)
    assert codes(only_contract) == {"CONTRACT_REGISTRATION_REQUIRED"}
    assert only_contract["plan"] is None


def test_coverage_gap_blocks_compilation(now):
    world = build_world(now)
    world["snapshot"] = replace(world["snapshot"], unresolved_paths=("nightly-batch",))
    signed(world)
    result = compile_(world)
    assert result["status"] == "NEEDS_EVIDENCE"
    assert "DISCOVERY_COVERAGE_INCOMPLETE" in codes(result)


@pytest.mark.parametrize(
    "effect,blocked",
    [
        ({"idempotency": "required", "result_lookup": "required", "on_unknown": "hold"}, False),
        ({"idempotency": "best_effort", "result_lookup": "required", "on_unknown": "hold"}, True),
        ({"idempotency": "required", "result_lookup": "unavailable", "on_unknown": "hold"}, True),
        ({"idempotency": "required", "result_lookup": "required", "on_unknown": "retry"}, True),
    ],
)
def test_external_effects_must_be_reconcilable_for_unattended_handoff(now, effect, blocked):
    data = example_service_contract().document
    data["spec"]["business"]["external_effects"] = [{"id": "shipment-request", **effect}]
    world = signed(build_world(now, contract=ServiceContract.from_document(data)))
    result = compile_(world)
    if blocked:
        assert result["status"] == "REQUIRES_REMEDIATION"
        assert "EXTERNAL_EFFECT_NOT_RECONCILABLE" in codes(result)
    else:
        assert result["status"] == "COMPILED"
        ids = {o["id"] for o in result["plan"]["obligations"]}
        assert "effect.shipment-request" in ids


def test_expired_qualification_or_clock_regression_never_compiles(world):
    expired = world["qualification"].expires_at
    world["clock"][0] = expired
    result = compile_(world, clock=lambda: expired)
    assert result["status"] == "NEEDS_EVIDENCE"
    world["clock"][0] = world["now"]
    times = iter([world["now"] + timedelta(seconds=5)] + [world["now"]] * 50)
    regressed = compile_(world, clock=lambda: next(times))
    assert regressed["status"] != "COMPILED"


def test_untyped_inputs_or_tenant_mismatch_are_rejected(world):
    with pytest.raises(ContractError):
        compile_(world, contract={"not": "typed"})
    with pytest.raises(ContractError):
        compile_(world, expected_tenant="another-tenant")
    with pytest.raises(ContractError):
        compile_(world, manifest=None)
