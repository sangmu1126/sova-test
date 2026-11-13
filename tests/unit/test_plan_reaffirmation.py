"""Re-evaluating a stale plan against a newer authenticated snapshot (INV-08)."""

from dataclasses import replace
from datetime import timedelta
import json

from change_support import memory_changes
from contract_world import build_world
import pytest

from sova.application.plan_reaffirmation import PlanReaffirmation
from sova.domain.change_impact import ChangeEvent, ChangeKind
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan
from sova.domain.system_graph import RelationKind, SystemRelation

NODES = ("api-a", "api-b", "database")


@pytest.fixture
def h(now):
    world = build_world(now)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, result = compile_revision(
        world["contract"], world["snapshot"], requested_nodes=NODES,
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    assert result["status"] == "COMPILED"
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    clock = {"now": now + timedelta(minutes=2)}
    feed, changes = memory_changes(lambda: clock["now"])
    feed.recorded.append(ChangeEvent(
        "ev-db-config", world["contract"].tenant_id, "another-service", ChangeKind.RESOURCE,
        "synthetic-source", "synthetic-uid-database", None, now + timedelta(seconds=20)))
    return dict(world=world, revision=revision, plan=plan, feed=feed, changes=changes,
                clock=clock, now=now)


def impact(h):
    return h["changes"](h["revision"], h["plan"], issued=0)


def newer_snapshot(h, *, at, signed=True, change=None):
    world = build_world(at, contract=h["world"]["contract"])
    snapshot = change(world["snapshot"]) if change else world["snapshot"]
    world["sign"](world["contract"])
    if signed:
        world["sign"](snapshot)
    return world, snapshot


def reaffirm(h, world, snapshot):
    return PlanReaffirmation(feed=h["feed"], clock=lambda: h["clock"]["now"]).reaffirm(
        h["revision"], h["plan"], world["contract"], snapshot,
        expected_tenant=world["contract"].tenant_id, authenticate=world["auth"],
        actor_id="planner")


def test_same_structure_reaffirms_the_plan_but_not_its_evidence(h):
    assert impact(h).plan_stale
    world, snapshot = newer_snapshot(h, at=h["now"] + timedelta(seconds=30))
    result = reaffirm(h, world, snapshot)
    assert result["status"] == "REAFFIRMED" and result["restores_evidence"] is False
    after = impact(h)
    assert after.commands_allowed
    assert after.structure_as_of == h["now"] + timedelta(seconds=30)
    # The fence probe observed before the change must still be re-run.
    assert not after.evidence_counts("fence.source_writes_denied",
                                     h["now"] + timedelta(seconds=10))
    # A change after the reaffirming snapshot makes the plan stale again.
    h["feed"].recorded.append(ChangeEvent(
        "ev-db-later", h["world"]["contract"].tenant_id, "another-service",
        ChangeKind.SCHEMA, "synthetic-source", "synthetic-uid-database", None,
        h["now"] + timedelta(seconds=40)))
    assert impact(h).plan_stale


def recreated_database(snapshot):
    return replace(snapshot, resource_uids=tuple(
        (n, u + "-recreated" if n == "database" else u) for n, u in snapshot.resource_uids))


def new_writer(snapshot):
    graph = snapshot.graph
    api = next(r for r in graph.resources if r.node_id == "api-a")
    batch = replace(api, node_id="batch", name="batch",
                    scope=replace(api.scope, resource_id="batch"))
    graph = replace(graph, resources=graph.resources + (batch,),
                    relations=graph.relations + (
                        SystemRelation("batch", "database", RelationKind.WRITES, True),))
    return replace(snapshot, graph=graph,
                   resource_uids=snapshot.resource_uids + (("batch", "synthetic-uid-batch"),))


@pytest.mark.parametrize("change,signed,offset,reason", [
    (recreated_database, True, 30, "UNIT_RESOURCES_CHANGED"),
    (new_writer, True, 30, "BOUNDARY_MERGE_REQUIRED"),
    (None, True, 0, "SNAPSHOT_NOT_NEWER"),
    (None, False, 30, "BOUNDARY_NEEDS_EVIDENCE"),
])
def test_anything_else_needs_a_new_plan(h, change, signed, offset, reason):
    world, snapshot = newer_snapshot(h, at=h["now"] + timedelta(seconds=offset),
                                     signed=signed, change=change)
    result = reaffirm(h, world, snapshot)
    assert result["status"] == "NEW_PLAN_REQUIRED"
    assert reason in result["reasons"]
    assert h["feed"].reaffirmations == {}  # nothing recorded
    assert impact(h).plan_stale


def test_adapter_changes_are_not_cleared_by_reaffirmation(h):
    h["feed"].recorded.clear()
    h["feed"].recorded.append(ChangeEvent(
        "ev-adapter", h["world"]["contract"].tenant_id, h["plan"].application_id,
        ChangeKind.ADAPTER, None, h["world"]["manifest"].adapter_id, None,
        h["now"] + timedelta(seconds=20)))
    world, snapshot = newer_snapshot(h, at=h["now"] + timedelta(seconds=30))
    assert reaffirm(h, world, snapshot)["status"] == "REAFFIRMED"
    assert impact(h).plan_stale
