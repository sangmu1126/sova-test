"""Synthetic boundary facts and authenticators; no live independence evidence."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from sova.adapters.registration.dependency_codec import decode_dependency_snapshot
from sova.application.boundary_examples import example_dependency_snapshot
from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError, ResourceScope
from sova.domain.migration_units import (
    BoundaryConstraint,
    CouplingKind,
    analyze_boundary,
)
from sova.domain.service_contracts import ServiceContract
from sova.domain.system_graph import RelationKind, SystemRelation
from sova.interfaces.cli import main


@pytest.fixture
def boundary_inputs(now):
    snapshot = replace(
        example_dependency_snapshot(now),
        writer_inventory_complete=True,
        control_inventory_complete=True,
        unresolved_paths=(),
    )
    return dict(
        contract=example_service_contract(),
        snapshot=snapshot,
        requested_nodes=("api-a",),
        expected_tenant="org-example",
        authenticate=lambda value: True,
        clock=lambda: now,
    )


def analyze(inputs, **changes):
    return analyze_boundary(**(inputs | changes))


def test_shared_inventory_merges_both_writers_and_leaves_read_only_audit_out(
    boundary_inputs,
):
    value = analyze(boundary_inputs)
    assert value["status"] == "MERGE_REQUIRED"
    assert value["members"] == ["api-a", "api-b", "database"]
    assert value["additional_members"] == ["api-b", "database"]
    assert "SHARED_WRITE_BOUNDARY" in value["coupling_reasons"]
    assert value["candidate_only"] is True
    assert value["executable"] is value["authorizes_execution"] is False
    assert value["boundary_links"] == [
        {"source": "api-a", "target": "audit", "kind": "reads", "required": True}
    ]
    complete = analyze(boundary_inputs, requested_nodes=("api-a", "api-b", "database"))
    assert complete["status"] == "PARTITIONABLE"
    assert complete["remaining_strategy_decisions"]


def test_read_cycle_does_not_force_scc_movement_but_ambiguous_calls_hold(
    boundary_inputs,
):
    snapshot = boundary_inputs["snapshot"]
    for kind, expected in [
        (RelationKind.READS, "PARTITIONABLE"),
        (RelationKind.CALLS, "NEEDS_EVIDENCE"),
    ]:
        graph = replace(
            snapshot.graph,
            relations=(
                SystemRelation("api-a", "api-b", kind, True),
                SystemRelation("api-b", "api-a", kind, True),
            ),
        )
        value = analyze(boundary_inputs, snapshot=replace(snapshot, graph=graph))
        assert value["status"] == expected
        assert value["members"] == ["api-a"]
        assert not value["additional_members"]
        if kind == RelationKind.CALLS:
            assert "CROSS_BOUNDARY_EFFECT_SEMANTICS_UNKNOWN" in value["blockers"]


def test_replication_crossing_the_unit_is_an_unresolved_decision(boundary_inputs):
    snapshot = boundary_inputs["snapshot"]
    graph = replace(
        snapshot.graph,
        relations=(
            SystemRelation("api-a", "database", RelationKind.WRITES, True),
            SystemRelation("database", "audit", RelationKind.REPLICATES, True),
        ),
    )
    value = analyze(
        boundary_inputs,
        snapshot=replace(snapshot, graph=graph),
        requested_nodes=("api-a", "database"),
    )
    assert value["status"] == "NEEDS_EVIDENCE"
    assert "CROSS_BOUNDARY_REPLICATION_UNRESOLVED" in value["blockers"]
    assert value["members"] == ["api-a", "database"]
    inside = analyze(
        boundary_inputs,
        snapshot=replace(snapshot, graph=graph),
        requested_nodes=("api-a", "audit", "database"),
    )
    assert "CROSS_BOUNDARY_REPLICATION_UNRESOLVED" not in inside["blockers"]


@pytest.mark.parametrize("kind", list(CouplingKind))
def test_explicit_atomic_idempotency_and_control_scopes_add_transitive_members(
    boundary_inputs, kind
):
    snapshot = boundary_inputs["snapshot"]
    constraint = BoundaryConstraint("constraint", kind, ("api-b", "audit"), "e" * 64)
    value = analyze(
        boundary_inputs, snapshot=replace(snapshot, constraints=(constraint,))
    )
    assert value["members"] == ["api-a", "api-b", "audit", "database"]
    assert str(kind).upper() in value["coupling_reasons"]


def test_database_wide_pause_cannot_masquerade_as_single_customer_control(
    boundary_inputs,
):
    snapshot = boundary_inputs["snapshot"]
    original_db = next(r for r in snapshot.graph.resources if r.node_id == "database")
    second_db = replace(
        original_db,
        node_id="database-b",
        name="database-b",
        scope=ResourceScope(
            "org-example", "orders-service", "synthetic-source", "database-b"
        ),
    )
    graph = replace(
        snapshot.graph,
        resources=snapshot.graph.resources + (second_db,),
        relations=(
            SystemRelation("api-a", "database", RelationKind.WRITES, True),
            SystemRelation("api-b", "database-b", RelationKind.WRITES, True),
        ),
    )
    snapshot = replace(
        snapshot,
        graph=graph,
        resource_uids=snapshot.resource_uids + (("database-b", "synthetic-db-b"),),
    )
    requested = ("api-a", "database")
    assert (
        analyze(boundary_inputs, snapshot=snapshot, requested_nodes=requested)["status"]
        == "PARTITIONABLE"
    )
    control = BoundaryConstraint(
        "pooler-pause-database",
        CouplingKind.CONTROL_SCOPE,
        ("database", "database-b"),
        "f" * 64,
    )
    value = analyze(
        boundary_inputs,
        snapshot=replace(snapshot, constraints=(control,)),
        requested_nodes=requested,
    )
    assert value["status"] == "MERGE_REQUIRED"
    assert value["additional_members"] == ["api-b", "database-b"]
    assert "CONTROL_SCOPE" in value["coupling_reasons"]


def test_shared_physical_pod_cannot_be_moved_as_independent_logical_components(
    boundary_inputs,
):
    snapshot = boundary_inputs["snapshot"]
    graph = replace(snapshot.graph, relations=())
    uids = dict(snapshot.resource_uids)
    uids["api-b"] = uids["api-a"]
    value = analyze(
        boundary_inputs,
        snapshot=replace(snapshot, graph=graph, resource_uids=tuple(uids.items())),
    )
    assert value["members"] == ["api-a", "api-b"]
    assert "SHARED_PHYSICAL_RESOURCE" in value["coupling_reasons"]


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("writer_inventory_complete", False, "WRITER_INVENTORY_INCOMPLETE"),
        ("control_inventory_complete", False, "CONTROL_INVENTORY_INCOMPLETE"),
        ("unresolved_paths", ("nightly-batch",), "DISCOVERY_COVERAGE_INCOMPLETE"),
        ("dataset_nodes", (), "PROTECTED_DATASET_MAPPING_INCOMPLETE"),
    ],
)
def test_missing_coverage_is_not_absence_of_dependencies(
    boundary_inputs, field, value, code
):
    snapshot = replace(boundary_inputs["snapshot"], **{field: value})
    result = analyze(boundary_inputs, snapshot=snapshot)
    assert result["status"] == "NEEDS_EVIDENCE"
    assert code in result["blockers"]


@pytest.mark.parametrize("answer", [None, False, 0, 1, "true", (True,)])
def test_no_truthy_or_missing_snapshot_authentication(boundary_inputs, answer):
    value = analyze(boundary_inputs, authenticate=lambda snapshot: answer)
    assert value["status"] == "NEEDS_EVIDENCE"
    assert "SNAPSHOT_AUTHENTICATION_REQUIRED" in value["blockers"]


def test_auth_error_is_not_leaked_and_scope_checks_precede_lookup(boundary_inputs):
    called = []

    def authenticate(snapshot):
        called.append(True)
        raise RuntimeError("must-not-leak-private-credential")

    result = analyze(boundary_inputs, authenticate=authenticate)
    assert "must-not-leak" not in json.dumps(result)
    assert called == [True]
    called.clear()
    with pytest.raises(ContractError):
        analyze(
            boundary_inputs, expected_tenant="other-tenant", authenticate=authenticate
        )
    assert not called
    with pytest.raises(ContractError):
        analyze(
            boundary_inputs,
            snapshot=replace(boundary_inputs["snapshot"], contract_digest="b" * 64),
        )


def test_expiry_and_clock_regression_cannot_complete_a_boundary(boundary_inputs, now):
    snapshot = boundary_inputs["snapshot"]
    assert (
        "SNAPSHOT_NOT_CURRENT"
        in analyze(boundary_inputs, clock=lambda: snapshot.graph.valid_until)[
            "blockers"
        ]
    )
    assert (
        "SNAPSHOT_NOT_CURRENT"
        in analyze(boundary_inputs, clock=lambda: now - timedelta(seconds=1))[
            "blockers"
        ]
    )
    times = iter((now, snapshot.graph.valid_until))
    assert (
        "SNAPSHOT_NOT_CURRENT"
        in analyze(boundary_inputs, clock=lambda: next(times))["blockers"]
    )
    times = iter((now + timedelta(seconds=2), now))
    assert (
        "CLOCK_REGRESSION"
        in analyze(boundary_inputs, clock=lambda: next(times))["blockers"]
    )


def test_snapshot_and_unit_hash_bind_uid_order_contract_and_constraints(
    boundary_inputs,
):
    snapshot = boundary_inputs["snapshot"]
    assert decode_dependency_snapshot(snapshot.document) == snapshot
    assert (
        replace(
            snapshot, resource_uids=tuple(reversed(snapshot.resource_uids))
        ).fingerprint
        == snapshot.fingerprint
    )
    first = analyze(boundary_inputs, requested_nodes=("api-a", "database"))
    assert (
        first["unit_digest"]
        == analyze(boundary_inputs, requested_nodes=("database", "api-a"))[
            "unit_digest"
        ]
    )
    uids = dict(snapshot.resource_uids)
    uids["api-a"] = "recreated-resource-with-same-name"
    changed = replace(snapshot, resource_uids=tuple(uids.items()))
    value = analyze(
        boundary_inputs,
        snapshot=changed,
        authenticate=lambda incoming: incoming.fingerprint == snapshot.fingerprint,
    )
    assert changed.fingerprint != snapshot.fingerprint
    assert value["status"] == "NEEDS_EVIDENCE"


def test_verified_partition_declaration_is_not_enforcement_qualification(
    boundary_inputs,
):
    data = boundary_inputs["contract"].document
    data["spec"]["migration"]["units"] = {
        "mode": "verified_partition",
        "partition_key": "customer_id",
        "partition_contract_ref": "partition-v1",
        "reject_cross_partition_transactions": True,
    }
    contract = ServiceContract.from_document(data)
    snapshot = replace(
        boundary_inputs["snapshot"], contract_digest=contract.fingerprint
    )
    value = analyze(
        boundary_inputs,
        contract=contract,
        snapshot=snapshot,
        requested_nodes=("api-a", "api-b", "database"),
    )
    assert value["status"] == "NEEDS_EVIDENCE"
    assert "PARTITION_ENFORCEMENT_NOT_QUALIFIED" in value["blockers"]


@pytest.mark.parametrize(
    "nodes", [(), ("api-a", "api-a"), ("unregistered",), ["api-a"]]
)
def test_request_cannot_silently_drop_unregistered_or_duplicate_members(
    boundary_inputs, nodes
):
    with pytest.raises(ContractError):
        analyze(boundary_inputs, requested_nodes=nodes)


def test_interchange_rejects_extra_flags_malformed_coverage_and_resource_mappings(
    boundary_inputs,
):
    data = boundary_inputs["snapshot"].document
    data["schema_version"] = True
    with pytest.raises(ContractError):
        decode_dependency_snapshot(data)
    data = boundary_inputs["snapshot"].document
    data["coverage"]["authenticated"] = True
    with pytest.raises(ContractError):
        decode_dependency_snapshot(data)
    with pytest.raises(ContractError):
        replace(boundary_inputs["snapshot"], resource_uids=(("api-a", "only-one-uid"),))
    with pytest.raises(ContractError):
        replace(boundary_inputs["snapshot"], writer_inventory_complete=1)


def test_cli_keeps_unsigned_example_unverified(tmp_path, capsys):
    assert main(["service-contract-example"]) == 0
    contract = tmp_path / "contract.json"
    contract.write_text(capsys.readouterr().out)
    assert main(["dependency-example"]) == 0
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(capsys.readouterr().out)
    assert (
        main(
            [
                "boundary-analyze",
                str(contract),
                str(snapshot),
                "--tenant",
                "org-example",
                "--node",
                "api-a",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "NEEDS_EVIDENCE"
    assert result["additional_members"] == ["api-b", "database"]
    assert result["executable"] is False
