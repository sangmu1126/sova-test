"""§12.5 / AT-38 cross-plan conflicts over synthetic, locally signed plans."""

from dataclasses import replace
import json

from contract_world import build_world
import pytest

from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError
from sova.domain.plan_compiler import PlanRevision, compile_revision
from sova.domain.plan_conflicts import detect_conflicts, lock_set
from sova.domain.service_contracts import ServiceContract


def plan_for(now, service="orders-service", uid_prefix=None):
    data = example_service_contract().document
    data["metadata"]["id"] = service
    world = build_world(now, contract=ServiceContract.from_document(data))
    if uid_prefix:
        world["snapshot"] = replace(
            world["snapshot"],
            resource_uids=tuple(
                (node, uid_prefix + uid) for node, uid in world["snapshot"].resource_uids
            ),
        )
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, result = compile_revision(
        world["contract"],
        world["snapshot"],
        requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id,
        source=world["source"],
        target=world["target"],
        manifest=world["manifest"],
        qualification=world["qualification"],
        rows=2000,
        authenticate=world["auth"],
        clock=lambda: now,
    )
    assert result["status"] == "COMPILED", result["blockers"]
    return revision


def test_two_services_sharing_one_physical_database_cannot_run_together(now):
    orders = plan_for(now, "orders-service")
    billing = plan_for(now, "billing-service")
    value = detect_conflicts(billing, (orders,))
    assert value["status"] == "CONFLICT"
    keys = {tuple(c["key"]) for c in value["conflicts"] if c["code"] == "OVERLAPPING_SCOPE"}
    assert ("source", "synthetic-source", "synthetic-uid-database") in keys
    assert value["executable"] is False


def test_disjoint_physical_resources_do_not_conflict(now):
    orders = plan_for(now, "orders-service")
    billing = plan_for(now, "billing-service", uid_prefix="billing-")
    assert detect_conflicts(billing, (orders,))["status"] == "NO_CONFLICT"
    assert detect_conflicts(orders, ())["status"] == "NO_CONFLICT"


def test_same_plan_twice_is_refused(now):
    orders = plan_for(now)
    value = detect_conflicts(orders, (orders,))
    assert [c["code"] for c in value["conflicts"]] == ["SAME_PLAN_ALREADY_ACTIVE"]


def test_lock_order_is_deterministic_and_write_dominates(now):
    orders = plan_for(now)
    locks, unresolved = lock_set(orders)
    assert not unresolved
    assert list(locks) == sorted(locks)
    modes = dict(locks)
    assert modes[("source", "synthetic-source", "synthetic-uid-database")] == "write"
    assert modes[("route", "org-example", "orders-service")] == "write"
    assert lock_set(orders) == lock_set(orders)


def observer_of(revision, service):
    """A hypothetical plan that only reads the same source database."""
    doc = json.loads(revision.canonical)
    doc["service_id"] = service
    for op in doc["operations"]:
        op["write_set"] = []
        op["read_set"] = [r for r in op["read_set"] if r == "source/database"]
    return PlanRevision(json.dumps(doc, sort_keys=True))


def test_read_only_overlap_is_not_a_conflict(now):
    orders = plan_for(now)
    first, second = observer_of(orders, "reader-a"), observer_of(orders, "reader-b")
    assert {m for _, m in lock_set(first)[0]} == {"read"}
    assert detect_conflicts(first, (second,))["status"] == "NO_CONFLICT"
    # Read against another plan's write on the same physical resource conflicts.
    assert detect_conflicts(first, (orders,))["status"] == "CONFLICT"


def test_unresolvable_scope_is_a_conflict_not_an_empty_lock(now):
    orders = plan_for(now)
    doc = json.loads(orders.canonical)
    doc["operations"][0]["write_set"].append("shared-network-policy/egress")
    widened = PlanRevision(json.dumps(doc, sort_keys=True))
    value = detect_conflicts(widened, ())
    assert value["status"] == "CONFLICT"
    assert value["conflicts"][0]["code"] == "SCOPE_UNRESOLVED"
    other = plan_for(now, "billing-service", uid_prefix="billing-")
    assert detect_conflicts(other, (widened,))["conflicts"][0]["code"] == (
        "ACTIVE_SCOPE_UNRESOLVED"
    )


def test_untyped_plans_are_refused(now):
    with pytest.raises(ContractError):
        detect_conflicts({"plan": "json"}, ())
    with pytest.raises(ContractError):
        detect_conflicts(plan_for(now), [plan_for(now)])
