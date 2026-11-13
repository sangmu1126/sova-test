"""Strict dependency interchange. A decoded snapshot is not authenticated."""

from sova.adapters.registration.graph_codec import decode_graph, exact
from sova.domain.contracts import ContractError
from sova.domain.migration_units import (
    BoundaryConstraint,
    CouplingKind,
    DependencySnapshot,
)


def decode_dependency_snapshot(data):
    try:
        exact(
            data,
            (
                "schema_version",
                "snapshot_id",
                "contract_digest",
                "graph",
                "resource_uids",
                "dataset_nodes",
                "coverage",
                "constraints",
            ),
        )
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ContractError("Unsupported dependency snapshot version")
        coverage = data["coverage"]
        exact(
            coverage,
            (
                "observed_paths",
                "unresolved_paths",
                "writer_inventory_complete",
                "control_inventory_complete",
            ),
        )
        if any(
            type(data[key]) is not dict for key in ("resource_uids", "dataset_nodes")
        ):
            raise ContractError("Resource UID and dataset mappings required")
        if type(data["constraints"]) is not list or len(data["constraints"]) > 512:
            raise ContractError("Bounded dependency constraints required")
        for key in ("observed_paths", "unresolved_paths"):
            if type(coverage[key]) is not list:
                raise ContractError("Coverage lists required")
        constraints = []
        for value in data["constraints"]:
            exact(value, ("id", "kind", "members", "evidence_digest"))
            if type(value["members"]) is not list:
                raise ContractError("Constraint member list required")
            constraints.append(
                BoundaryConstraint(
                    value["id"],
                    CouplingKind(value["kind"]),
                    tuple(value["members"]),
                    value["evidence_digest"],
                )
            )
        return DependencySnapshot(
            data["snapshot_id"],
            data["contract_digest"],
            decode_graph(data["graph"]),
            tuple(data["resource_uids"].items()),
            tuple(data["dataset_nodes"].items()),
            tuple(coverage["observed_paths"]),
            tuple(coverage["unresolved_paths"]),
            coverage["writer_inventory_complete"],
            coverage["control_inventory_complete"],
            tuple(constraints),
        )
    except (TypeError, KeyError, ValueError, OverflowError) as exc:
        raise ContractError("Invalid dependency snapshot") from exc
