"""ADR-0002: the account set is its own handoff unit, ordered before the data unit."""

import json

from contract_world import build_world
from test_identity_contract import MIGRATE, contract_with

from sova.domain.migration_units import analyze_boundary
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan

IDP = "sign-in"
DATA = ("api-a", "api-b", "database")


def world_for(now, contract, *, identity_provider=IDP):
    world = build_world(now, contract=contract, identity_provider=identity_provider)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    return world


def compiled(world, nodes, now):
    return compile_revision(
        world["contract"], world["snapshot"], requested_nodes=nodes,
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now)


def obligations(revision):
    return {o["id"]: o for o in json.loads(revision.canonical)["obligations"]}


def step(obligation):
    """The step an obligation gates (operation IDs carry a unit prefix)."""
    return obligation["required_at"].rsplit(".", 1)[1]


def test_the_data_unit_waits_for_the_account_unit(now):
    world = world_for(now, contract_with(MIGRATE))
    revision, result = compiled(world, DATA, now)
    assert result["status"] == "COMPILED", result["blockers"]
    accounts = analyze_boundary(world["contract"], world["snapshot"], requested_nodes=(IDP,),
                                expected_tenant=world["contract"].tenant_id,
                                authenticate=world["auth"], clock=lambda: now)
    gate = obligations(revision)["identity.unit_handoff_complete"]
    assert gate["kind"] == "unit_handoff_complete"
    assert step(gate) == "record_activation_intent"  # before any target write
    assert gate["unit_digest"] == accounts["unit_digest"] and gate["members"] == [IDP]
    # The identity gate the data unit already had stays: mapping checked again.
    assert {"IAM-01.subject_mapping_unique", "IAM-02.fresh_login_and_validation"} <= set(
        obligations(revision))
    lower_plan(revision, created_by="planner",
               verifiers={o["kind"]: "verifier" for o in obligations(revision).values()})


def test_the_account_unit_is_a_plan_of_the_same_protocol(now):
    world = world_for(now, contract_with(MIGRATE))
    revision, result = compiled(world, (IDP,), now)
    assert result["status"] == "COMPILED", result["blockers"]
    doc = json.loads(revision.canonical)
    data_revision, _ = compiled(world, DATA, now)
    assert doc["unit"]["unit_digest"] == obligations(data_revision)[
        "identity.unit_handoff_complete"]["unit_digest"]
    found = obligations(revision)
    # Business data is not part of this unit; accounts are compared instead.
    assert not [i for i in found if i.startswith(("data.", "business.", "effect."))]
    assert step(found["IAM-01.subject_mapping_unique"]) == "verify_final_state"
    assert step(found["IAM-06.account_state_parity"]) == "verify_final_state"
    # Sign-in at the target opens with this unit's activation: flow tests follow it.
    for flow in ("IAM-02.fresh_login_and_validation", "IAM-03.mfa_and_recovery",
                 "IAM-04.cross_tenant_denial"):
        assert step(found[flow]) == "stabilize", flow
    assert "identity.unit_handoff_complete" not in found
    assert [op["kind"] for op in doc["operations"]][:3] == [
        "stage_target", "pause_admission", "fence_source_writes"]


def test_crossing_into_an_identity_provider_stays_undecided_without_migration(now):
    """Keeping the source IdP leaves a service-to-IdP call across the unit as an
    unknown cross-boundary effect, exactly as before."""
    retain = dict(MIGRATE, remove_dependencies=[], allowed_external_idps=["target-oidc"])
    world = world_for(now, contract_with(retain))
    _, data = compiled(world, DATA, now)
    assert "CROSS_BOUNDARY_EFFECT_SEMANTICS_UNKNOWN" in {b["code"] for b in data["blockers"]}
    _, accounts = compiled(world, (IDP,), now)
    codes = {b["code"] for b in accounts["blockers"]}
    assert accounts["status"] == "UNSUPPORTED"
    assert "IDENTITY_UNIT_REQUIRES_MIGRATE_IDENTITY" in codes


def test_without_an_identity_node_the_plan_is_unchanged(now):
    """An inventory that does not register the IdP keeps the earlier form: the
    account move stays outside the plan, and the compiled plan is identical."""
    contract = contract_with(MIGRATE)
    plain = world_for(now, contract, identity_provider=None)
    revision, result = compiled(plain, DATA, now)
    assert result["status"] == "COMPILED"
    assert "identity.unit_handoff_complete" not in obligations(revision)


def test_units_lock_apart_and_one_identity_provider_moves_once(now):
    """The account unit's route is the issuer the application trusts, not the
    data route, so the two units of one service do not block each other; two
    plans moving the same identity provider do (ADR-0002, plan conflicts)."""
    from sova.domain.plan_conflicts import detect_conflicts, lock_set

    world = world_for(now, contract_with(MIGRATE))
    accounts, _ = compiled(world, (IDP,), now)
    data, _ = compiled(world, DATA, now)
    keys = dict(lock_set(accounts)[0])
    assert keys[("route", world["contract"].tenant_id, world["contract"].service_id,
                 "identity")] == "write"
    assert any(k[0] == "source" and k[2].endswith(IDP) and mode == "write"
               for k, mode in keys.items())  # the fence writes the IdP's write policy
    assert detect_conflicts(data, (accounts,))["status"] == "NO_CONFLICT"
    # Another plan for the same accounts (here another strategy) is a conflict.
    other = build_world(now, contract=contract_with(MIGRATE), strategy="snapshot-cdc",
                        identity_provider=IDP)
    for subject in (other["contract"], other["snapshot"], other["qualification"]):
        other["sign"](subject)
    again, _ = compiled(other, (IDP,), now)
    assert again.digest != accounts.digest
    found = detect_conflicts(again, (accounts,))
    assert found["status"] == "CONFLICT"
    assert {c["code"] for c in found["conflicts"]} == {"OVERLAPPING_SCOPE"}
