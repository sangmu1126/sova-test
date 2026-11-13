"""Evidence-bound minimum boundary proposals, not deployment/SCC groups.

Ownership, evidence authenticity, and completeness are supplied by an authorized
registry/verifier. This analyzer never discovers omitted writers or grants an
execution right. Ambiguous calls and unqualified partition enforcement hold.
"""

from dataclasses import dataclass
from enum import StrEnum
import hashlib

from sova.domain.contracts import ContractError, canonical_json, digest, identifier, utc
from sova.domain.service_contracts import ServiceContract
from sova.domain.system_graph import RelationKind, SystemGraph


MAX_SNAPSHOT_BYTES = 1048576


def _names(items, *, minimum=0, maximum=512):
    if type(items) is not tuple or not minimum <= len(items) <= maximum:
        raise ContractError("Bounded immutable boundary identifiers required")
    for item in items:
        identifier(item)
    if len(set(items)) != len(items):
        raise ContractError("Duplicate boundary identifier")
    return tuple(sorted(items))


def _pairs(items, *, maximum=512):
    if type(items) is not tuple or len(items) > maximum:
        raise ContractError("Bounded immutable boundary mappings required")
    keys = set()
    for item in items:
        if type(item) is not tuple or len(item) != 2:
            raise ContractError("Immutable boundary mapping pair required")
        for value in item:
            identifier(value)
        if item[0] in keys:
            raise ContractError("Ambiguous boundary mapping")
        keys.add(item[0])
    return tuple(sorted(items))


class CouplingKind(StrEnum):
    ATOMIC_TRANSACTION = "atomic_transaction"
    SHARED_WRITER_AUTHORITY = "shared_writer_authority"
    SHARED_IDEMPOTENCY_STATE = "shared_idempotency_state"
    CONTROL_SCOPE = "control_scope"


@dataclass(frozen=True, slots=True)
class BoundaryConstraint:
    constraint_id: str
    kind: CouplingKind
    members: tuple[str, ...]
    evidence_digest: str

    def __post_init__(self):
        identifier(self.constraint_id)
        if not isinstance(self.kind, CouplingKind):
            raise ContractError("Typed boundary coupling required")
        object.__setattr__(
            self, "members", _names(self.members, minimum=1, maximum=128)
        )
        digest(self.evidence_digest)

    @property
    def document(self):
        return {
            "id": self.constraint_id,
            "kind": str(self.kind),
            "members": list(self.members),
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class DependencySnapshot:
    snapshot_id: str
    contract_digest: str
    graph: SystemGraph
    resource_uids: tuple[tuple[str, str], ...]
    dataset_nodes: tuple[tuple[str, str], ...]
    observed_paths: tuple[str, ...]
    unresolved_paths: tuple[str, ...]
    writer_inventory_complete: bool
    control_inventory_complete: bool
    constraints: tuple[BoundaryConstraint, ...] = ()

    def __post_init__(self):
        identifier(self.snapshot_id)
        digest(self.contract_digest)
        if not isinstance(self.graph, SystemGraph):
            raise ContractError("Typed dependency graph required")
        for field in ("writer_inventory_complete", "control_inventory_complete"):
            if type(getattr(self, field)) is not bool:
                raise ContractError("Exact inventory completeness boolean required")
        object.__setattr__(
            self, "resource_uids", _pairs(self.resource_uids, maximum=128)
        )
        object.__setattr__(self, "dataset_nodes", _pairs(self.dataset_nodes))
        object.__setattr__(
            self, "observed_paths", _names(self.observed_paths, minimum=1)
        )
        object.__setattr__(self, "unresolved_paths", _names(self.unresolved_paths))
        nodes = {r.node_id for r in self.graph.resources}
        if set(dict(self.resource_uids)) != nodes:
            raise ContractError("Every graph node must bind a resource UID")
        if any(node not in nodes for _, node in self.dataset_nodes):
            raise ContractError("Dataset mapping references an unknown node")
        if type(self.constraints) is not tuple or len(self.constraints) > 512:
            raise ContractError("Bounded immutable boundary constraints required")
        ids = set()
        for constraint in self.constraints:
            if (
                not isinstance(constraint, BoundaryConstraint)
                or not set(constraint.members) <= nodes
            ):
                raise ContractError("Constraint references an unknown member")
            if constraint.constraint_id in ids:
                raise ContractError("Duplicate boundary constraint")
            ids.add(constraint.constraint_id)
        object.__setattr__(
            self,
            "constraints",
            tuple(sorted(self.constraints, key=lambda c: c.constraint_id)),
        )
        canonical_json(self.document, limit=MAX_SNAPSHOT_BYTES)

    @property
    def document(self):
        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "contract_digest": self.contract_digest,
            "graph": self.graph.document,
            "resource_uids": dict(self.resource_uids),
            "dataset_nodes": dict(self.dataset_nodes),
            "coverage": {
                "observed_paths": list(self.observed_paths),
                "unresolved_paths": list(self.unresolved_paths),
                "writer_inventory_complete": self.writer_inventory_complete,
                "control_inventory_complete": self.control_inventory_complete,
            },
            "constraints": [c.document for c in self.constraints],
        }

    @property
    def fingerprint(self):
        return hashlib.sha256(
            canonical_json(self.document, limit=MAX_SNAPSHOT_BYTES).encode()
        ).hexdigest()


def analyze_boundary(
    contract, snapshot, *, requested_nodes, expected_tenant, authenticate, clock
):
    if not isinstance(contract, ServiceContract) or not isinstance(
        snapshot, DependencySnapshot
    ):
        raise ContractError("Typed service contract and dependency snapshot required")
    contract.inspect(expected_tenant=expected_tenant)
    graph = snapshot.graph
    if (graph.tenant_id, graph.application_id, snapshot.contract_digest) != (
        expected_tenant,
        contract.service_id,
        contract.fingerprint,
    ):
        raise ContractError("Snapshot does not belong to this service contract")
    selected = _names(requested_nodes, minimum=1, maximum=128)
    nodes = {r.node_id: r for r in graph.resources}
    if not set(selected) <= set(nodes):
        raise ContractError("Requested boundary contains an unknown node")
    if not callable(clock):
        raise ContractError("Trusted boundary clock required")
    now = utc(clock())
    parent = {node: node for node in nodes}
    reasons = []

    def root(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def couple(members, reason):
        # Deterministic union/find over positive, explicitly supplied constraints.
        representative = min(root(node) for node in members)
        for node in members:
            parent[root(node)] = representative
        reasons.append((tuple(sorted(members)), reason))

    for relation in graph.relations:
        if relation.kind == RelationKind.WRITES:
            couple((relation.source, relation.target), "SHARED_WRITE_BOUNDARY")
    physical = {}
    uids = dict(snapshot.resource_uids)
    for node, resource in nodes.items():
        physical.setdefault((resource.scope.environment_id, uids[node]), []).append(
            node
        )
    for members in physical.values():
        if len(members) > 1:
            couple(members, "SHARED_PHYSICAL_RESOURCE")
    for constraint in snapshot.constraints:
        couple(constraint.members, str(constraint.kind).upper())
    roots = {root(node) for node in selected}
    members = tuple(sorted(node for node in nodes if root(node) in roots))
    boundary_links = [
        relation.document
        for relation in graph.relations
        if (relation.source in members) != (relation.target in members)
    ]
    blockers = []
    if not graph.observed_at <= now < graph.valid_until:
        blockers.append("SNAPSHOT_NOT_CURRENT")
    if not snapshot.writer_inventory_complete:
        blockers.append("WRITER_INVENTORY_INCOMPLETE")
    if not snapshot.control_inventory_complete:
        blockers.append("CONTROL_INVENTORY_INCOMPLETE")
    if snapshot.unresolved_paths:
        blockers.append("DISCOVERY_COVERAGE_INCOMPLETE")
    required_sets = set(contract.document["spec"]["data"]["protected_sets"])
    if set(dict(snapshot.dataset_nodes)) != required_sets:
        blockers.append("PROTECTED_DATASET_MAPPING_INCOMPLETE")
    crossing = {link["kind"] for link in boundary_links}
    if crossing & {str(RelationKind.CALLS), str(RelationKind.USES)}:
        blockers.append("CROSS_BOUNDARY_EFFECT_SEMANTICS_UNKNOWN")
    if str(RelationKind.REPLICATES) in crossing:
        # Keeping, cutting, or reversing a replication channel that crosses the
        # proposed unit is a §10.1 step-5 decision; a crossing copy is not inert.
        blockers.append("CROSS_BOUNDARY_REPLICATION_UNRESOLVED")
    authenticated = False
    if not blockers and callable(authenticate):
        try:
            authenticated = authenticate(snapshot) is True
        except Exception:
            authenticated = False
    if not authenticated:
        blockers.append("SNAPSHOT_AUTHENTICATION_REQUIRED")
    confirmed_at = utc(clock())
    if confirmed_at < now:
        blockers.append("CLOCK_REGRESSION")
    if not graph.observed_at <= confirmed_at < graph.valid_until:
        blockers.append("SNAPSHOT_NOT_CURRENT")
    if blockers:
        status = "NEEDS_EVIDENCE"
    elif members != selected:
        status = "MERGE_REQUIRED"
    elif (
        contract.document["spec"]["migration"]["units"]["mode"] == "verified_partition"
    ):
        status = "NEEDS_EVIDENCE"
        blockers.append("PARTITION_ENFORCEMENT_NOT_QUALIFIED")
    else:
        status = "PARTITIONABLE"
    unit_document = {
        "analysis_version": "business-boundary.v1",
        "contract_digest": contract.fingerprint,
        "snapshot_digest": snapshot.fingerprint,
        "requested": list(selected),
        "members": list(members),
    }
    unit_digest = hashlib.sha256(canonical_json(unit_document).encode()).hexdigest()
    return {
        "schema_version": 1,
        "status": status,
        "candidate_only": True,
        "contract_digest": contract.fingerprint,
        "snapshot_digest": snapshot.fingerprint,
        "unit_digest": unit_digest,
        "requested_nodes": list(selected),
        "members": list(members),
        "additional_members": sorted(set(members) - set(selected)),
        "coupling_reasons": sorted(
            {reason for group, reason in reasons if set(group) & set(members)}
        ),
        "boundary_links": boundary_links,
        "remaining_strategy_decisions": [
            "CROSS_BOUNDARY_LINK_POLICY",
            "DIRECTIONAL_CAPABILITY",
            "SOURCE_FENCE_QUALIFICATION",
            "DATA_AND_BUSINESS_VERIFICATION",
        ],
        "blockers": sorted(set(blockers)),
        "evaluated_at": confirmed_at.isoformat(),
        "executable": False,
        "authorizes_execution": False,
    }
