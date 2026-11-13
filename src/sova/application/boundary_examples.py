"""Synthetic shared-write counterexample, deliberately unqualified coverage."""

from datetime import timedelta

from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ResourceScope
from sova.domain.migration_units import DependencySnapshot
from sova.domain.system_graph import (
    Portability,
    RelationKind,
    ResourceKind,
    SystemGraph,
    SystemRelation,
    SystemResource,
)


def example_dependency_snapshot(now):
    contract = example_service_contract()
    kinds = {
        "api-a": ResourceKind.SERVICE,
        "api-b": ResourceKind.SERVICE,
        "database": ResourceKind.DATABASE,
        "audit": ResourceKind.OBSERVABILITY,
    }
    graph = SystemGraph(
        contract.tenant_id,
        contract.service_id,
        1,
        "registration",
        now,
        now + timedelta(hours=1),
        tuple(
            SystemResource(
                node,
                node,
                ResourceScope(
                    contract.tenant_id, contract.service_id, "synthetic-source", node
                ),
                kind,
                "synthetic-not-live",
                "example-region",
                "synthetic-revision",
                Portability.UNKNOWN,
            )
            for node, kind in kinds.items()
        ),
        (
            SystemRelation("api-a", "database", RelationKind.WRITES, True),
            SystemRelation("api-b", "database", RelationKind.WRITES, True),
            SystemRelation("api-a", "audit", RelationKind.READS, True),
        ),
    )
    return DependencySnapshot(
        "synthetic-shared-write",
        contract.fingerprint,
        graph,
        tuple((node, "synthetic-uid-" + node) for node in kinds),
        tuple(
            (dataset, "database")
            for dataset in contract.document["spec"]["data"]["protected_sets"]
        ),
        ("synthetic-normal-path",),
        ("nightly-batch", "external-effects", "source-restore"),
        False,
        False,
    )
