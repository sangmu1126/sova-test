"""Re-evaluate a stale plan against a newer snapshot (INV-08 re-evaluation).

A resource-level change makes a plan stale because its snapshot no longer
describes the unit. Re-evaluation re-runs the evidence-bound boundary analysis
on a newer, authenticated snapshot. If the unit is still partitionable with
exactly the same members, kinds, environments, and resource UIDs, and the
contract is the one compiled in, the plan's structure is reaffirmed as of that
snapshot. Anything else needs a new plan and a new approval.

Reaffirmation never restores evidence: obligations invalidated by the change
must still be re-verified. It does not cover adapter, unclassified, or
registry changes, which change what the plan would compile to.
"""

from datetime import datetime
import json

from sova.domain.contracts import ContractError, utc
from sova.domain.migration_units import DependencySnapshot, analyze_boundary
from sova.domain.plan_compiler import PlanRevision
from sova.domain.service_contracts import ServiceContract


def reaffirm_structure(revision, contract, snapshot, *, expected_tenant, authenticate, clock):
    if not isinstance(revision, PlanRevision) or not isinstance(contract, ServiceContract):
        raise ContractError("Frozen compiled plan and typed contract required")
    if not isinstance(snapshot, DependencySnapshot):
        raise ContractError("Typed dependency snapshot required")
    doc = json.loads(revision.canonical)
    members = list(doc["unit"]["members"])
    reasons = []
    if contract.fingerprint != doc["inputs"]["contract_digest"]:
        reasons.append("CONTRACT_CHANGED")
    boundary = analyze_boundary(
        contract, snapshot, requested_nodes=tuple(members), expected_tenant=expected_tenant,
        authenticate=authenticate, clock=clock,
    )
    if boundary["status"] != "PARTITIONABLE":
        reasons.append("BOUNDARY_" + boundary["status"])
        reasons.extend("BOUNDARY_" + code for code in boundary["blockers"])
    if sorted(boundary["members"]) != sorted(members):
        reasons.append("UNIT_MEMBERS_CHANGED")
    kinds = {r.node_id: str(r.kind) for r in snapshot.graph.resources}
    envs = {r.node_id: r.scope.environment_id for r in snapshot.graph.resources}
    uids = dict(snapshot.resource_uids)
    current = {n: {"kind": kinds.get(n), "environment": envs.get(n),
                   "resource_uid": uids.get(n)} for n in members}
    if current != doc["resources"]:
        reasons.append("UNIT_RESOURCES_CHANGED")
    observed = utc(snapshot.graph.observed_at)
    if observed <= datetime.fromisoformat(doc["inputs"]["snapshot_observed_at"]):
        reasons.append("SNAPSHOT_NOT_NEWER")
    return {
        "schema_version": 1,
        "status": "NEW_PLAN_REQUIRED" if reasons else "REAFFIRMED",
        "reasons": sorted(set(reasons)),
        "compiled_digest": revision.digest,
        "snapshot_digest": snapshot.fingerprint,
        "observed_at": observed.isoformat(),
        "restores_evidence": False,
    }
