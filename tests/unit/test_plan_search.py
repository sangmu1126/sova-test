"""Candidate strategy search: feasible, not satisfied, or undecided (§12.3, §12.4)."""

from dataclasses import replace

from contract_world import build_world
import pytest

from sova.domain.capability_catalog import INCREMENTAL_FINAL_CHANGES, CapabilityManifest
from sova.domain.contracts import ContractError
from sova.domain.plan_compiler import required_operations
from sova.domain.plan_search import MAX_CANDIDATES, search_plans
from sova.domain.service_contracts import MigrationProfile

NODES = ("api-a", "api-b", "database")


def candidate(now, strategy):
    world = build_world(now, strategy=strategy)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    return world


def search(world, candidates, now):
    return search_plans(
        world["contract"], world["snapshot"], candidates, requested_nodes=NODES,
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], rows=2000, authenticate=world["auth"], clock=lambda: now)


def pair(world):
    return (world["manifest"], world["qualification"])


def test_feasible_candidates_are_ranked_by_the_fenced_window(now):
    copy = candidate(now, "offline-copy")
    # A second strategy qualified in the same registry for the same contract,
    # whose final changes were qualified as incremental.
    cdc = build_world(now, strategy="snapshot-cdc", contract=copy["contract"],
                      properties=(INCREMENTAL_FINAL_CHANGES,))
    copy["sign"](cdc["qualification"])
    revisions, result = search(copy, (pair(copy), pair(cdc)), now)
    assert result["status"] == "COMPILED" and result["search_complete"]
    by_strategy = {c["strategy_id"]: c for c in result["candidates"]}
    assert by_strategy["offline-copy"]["full_copy_in_fenced_window"] is True
    assert by_strategy["offline-copy"]["fenced_window_basis"] == (
        "FULL_COPY_STEP_IN_FENCED_WINDOW")
    assert by_strategy["snapshot-cdc"]["full_copy_in_fenced_window"] is False
    assert by_strategy["snapshot-cdc"]["fenced_window_basis"] == (
        "QUALIFIED_INCREMENTAL_FINAL_CHANGES")
    assert result["recommended"] == by_strategy["snapshot-cdc"]["plan_digest"]
    assert set(result["feasible"]) == set(revisions)
    assert result["requirements_relaxed"] is False
    assert result["authorizes_execution"] is False


def test_a_final_changes_step_alone_earns_no_rank(now):
    """The reference adapters apply the final changes by a full re-copy: a
    snapshot-cdc plan whose manifest does not claim incremental final changes,
    or whose qualification was not issued for that claim, is ranked as copying
    in the fenced window - measured, it paused writes longer, not shorter."""
    copy = candidate(now, "offline-copy")
    undeclared = build_world(now, strategy="snapshot-cdc", contract=copy["contract"])
    claimed = build_world(now, strategy="snapshot-cdc", contract=copy["contract"],
                          properties=(INCREMENTAL_FINAL_CHANGES,))
    unqualified = replace(claimed["qualification"], properties=())
    for qualification in (undeclared["qualification"], unqualified):
        copy["sign"](qualification)
    for cdc in ((undeclared["manifest"], undeclared["qualification"]),
                (claimed["manifest"], unqualified)):
        _, result = search(copy, (pair(copy), cdc), now)
        assert result["status"] == "COMPILED"
        by_strategy = {c["strategy_id"]: c for c in result["candidates"]}
        assert by_strategy["snapshot-cdc"]["full_copy_in_fenced_window"] is True
        assert by_strategy["snapshot-cdc"]["fenced_window_basis"] == (
            "FINAL_CHANGES_NOT_QUALIFIED_INCREMENTAL")
        # No basis to prefer either: the order falls back to the stable tie-break.
        assert result["recommended"] == by_strategy["offline-copy"]["plan_digest"]


def test_no_feasible_plan_only_when_every_candidate_was_decided(now):
    world = candidate(now, "offline-copy")
    lacking = CapabilityManifest(
        "pg-partial", "1.0.0", "a" * 64, "offline-copy", "1.0.0", ("stage_target",),
        (MigrationProfile.SINGLE_AUTHORITY,))
    _, result = search(world, ((lacking, world["qualification"]),), now)
    assert result["status"] == "NO_FEASIBLE_PLAN" and result["search_complete"]
    assert result["candidates"][0]["status"] == "UNSUPPORTED"
    assert result["recommended"] is None and result["approval_available"] is False


def test_missing_evidence_or_bad_input_leaves_the_search_incomplete(now):
    world = candidate(now, "offline-copy")
    unsigned = build_world(now, strategy="snapshot-cdc", contract=world["contract"])
    _, result = search(world, (pair(unsigned),), now)
    assert result["status"] == "SEARCH_INCOMPLETE" and not result["search_complete"]
    _, invalid = search(world, ((object(), None),), now)
    assert invalid["candidates"][0]["status"] == "INPUT_INVALID"
    assert invalid["status"] == "SEARCH_INCOMPLETE"


def test_the_search_budget_is_reported_not_hidden(now):
    world = candidate(now, "offline-copy")
    many = (pair(world),) * (MAX_CANDIDATES + 2)
    _, result = search(world, many, now)
    assert result["evaluated"] == MAX_CANDIDATES and result["not_evaluated"] == 2
    assert result["status"] == "COMPILED" and result["search_complete"] is False
    with pytest.raises(ContractError, match="own manifest"):
        search_plans(world["contract"], world["snapshot"], (pair(world),),
                     manifest=world["manifest"])
    assert required_operations("snapshot-cdc")
