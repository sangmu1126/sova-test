"""C0-C11 plan compilation for PLANNED_SINGLE_AUTHORITY. Never authorizes.

The compiler re-derives every input decision itself instead of trusting result
dictionaries: contract registration (C0), the evidence-bound boundary (C1-C2)
and the directional qualification (C3) are evaluated here with the caller's
authenticator and trusted clock. It then checks the contract constraints it can
decide (C4), expands a strategy template into a typed handoff DAG (C5-C7),
binds verification obligations and recovery boundaries (C8), checks internal
consistency (C10), and freezes a digest over the semantic inputs (C11).

Constraints that need measurements (target security controls, residency,
write-pause budget, restore test) become execution-gate obligations; they are
never assumed satisfied. Lowering to environment commands (C9) and approval
(C12) are separate. A COMPILED plan is not an execution right.
"""

from dataclasses import dataclass
import hashlib
import json

from sova.domain.capability_catalog import (
    CapabilityManifest,
    EnvironmentProfile,
    EvidenceGrade,
    StrategyQualification,
    assess_strategy,
)
from sova.domain.contracts import ContractError, canonical_json, utc
from sova.domain.migration_units import DependencySnapshot, analyze_boundary
from sova.domain.service_contracts import MigrationProfile, ServiceContract
from sova.domain.system_graph import ResourceKind


COMPILER_VERSION = "sova-plan-compiler.v1"
MAX_PLAN_BYTES = 262144
PRE, POST = "pre_activation", "post_activation_intent"


@dataclass(frozen=True, slots=True)
class TemplateStep:
    step_id: str
    phase: str  # §15.2 handoff state reached when the step's ensures hold
    operation: str | None  # adapter operation; None is a platform-owned step
    requires: tuple[str, ...]
    ensures: tuple[str, ...]
    boundary: str


def _step(step_id, phase, operation, requires, ensures, boundary=PRE):
    return TemplateStep(step_id, phase, operation, requires, ensures, boundary)


_STAGE = _step(
    "stage_target", "TARGET_PREPARED", "stage_target",
    ("approved_preparation_scope", "residency_verified"),
    ("target_prepared", "target_business_writes_disabled"),
)
_PAUSE = _step(
    "pause_admission", "QUIESCING", "pause_admission",
    ("approval_matches_plan_revision", "cutover_budget_current",
     "external_effects_reconcilable"),
    ("new_admission_paused", "inflight_work_classified"),
)
_FENCE = _step(
    "fence_source_writes", "SOURCE_FENCED", "fence_writes",
    ("new_admission_paused",),
    ("source_fence_verified_and_latched",),
)
_VERIFY = _step(
    "verify_final_state", "FINAL_STATE_VERIFIED", "verify_content",
    ("source_fence_verified_and_latched", "final_changes_applied"),
    ("business_consistency_verified",),
)
_INTENT = _step(
    "record_activation_intent", "TARGET_ACTIVATION_INTENT", None,
    ("business_consistency_verified", "destination_access_controls_verified",
     "approval_matches_plan_revision"),
    ("activation_intent_durable",),
    POST,
)
_ACTIVATE = _step(
    "activate_target", "TARGET_ACTIVE_OBSERVED", "activate_writer",
    ("activation_intent_durable", "source_fence_verified_and_latched"),
    ("destination_business_writes_enabled_for_current_epoch",
     "source_business_writes_remain_fenced"),
    POST,
)
_ROUTE = _step(
    "commit_route", "ROUTE_CONFIRMED", "commit_route",
    ("destination_business_writes_enabled_for_current_epoch",),
    ("authenticated_route_confirmed", "stale_route_writes_rejected"),
    POST,
)
_STABILIZE = _step(
    "stabilize", "STABILIZING", None,
    ("authenticated_route_confirmed",),
    ("contract_measurements_recorded",),
    POST,
)

TEMPLATES = {
    # §16.1 Offline Copy: the copy happens inside the fenced window.
    "offline-copy": (
        _STAGE, _PAUSE, _FENCE,
        _step("copy_content", "SOURCE_FENCED", "copy_content",
              ("source_fence_verified_and_latched",), ("final_changes_applied",)),
        _VERIFY, _INTENT, _ACTIVATE, _ROUTE, _STABILIZE,
    ),
    # §16.1 Snapshot + CDC: bulk copy before quiesce, final changes after fence.
    "snapshot-cdc": (
        _STAGE,
        _step("start_sync", "SYNCING", "start_sync",
              ("target_prepared",), ("replication_scope_and_start_point_recorded",)),
        _step("check_cutover_ready", "CUTOVER_READY", None,
              ("replication_scope_and_start_point_recorded",),
              ("cutover_budget_current",)),
        _PAUSE, _FENCE,
        _step("apply_final_changes", "SOURCE_FENCED", "apply_final_changes",
              ("source_fence_verified_and_latched",), ("final_changes_applied",)),
        _VERIFY, _INTENT, _ACTIVATE, _ROUTE, _STABILIZE,
    ),
}


def required_operations(strategy_id):
    template = TEMPLATES.get(strategy_id)
    if template is None:
        return None
    return tuple(sorted({s.operation for s in template if s.operation}))


@dataclass(frozen=True, slots=True)
class PlanRevision:
    """Frozen, canonical plan IR. Its digest excludes evaluation time."""

    canonical: str

    @property
    def digest(self):
        return hashlib.sha256(self.canonical.encode()).hexdigest()


def _blocker(stage, code, **detail):
    return {"stage": stage, "code": code, **detail}


def _reconcilable(effect):
    return (
        effect["idempotency"] == "required"
        and effect["result_lookup"] == "required"
        and effect["on_unknown"] == "hold"
    )


def _obligations(spec, steps):
    at = {s.step_id: s for s in steps}

    def ob(obligation_id, kind, step_id, **extra):
        if step_id not in at:
            raise ContractError("Compiler consistency violation")
        return {"id": obligation_id, "kind": kind, "required_at": step_id, **extra}

    result = [
        ob("fence.source_writes_denied", "independent_write_denial_probe",
           "fence_source_writes", covers="all_registered_writer_paths"),
        ob("authority.single_writer_after_activation", "authority_readback",
           "activate_target"),
        ob("route.stale_writes_rejected", "stale_route_probe", "commit_route"),
    ]
    for category, zones in sorted(spec["residency"].items()):
        if zones:
            result.append(ob(f"residency.{category}", "placement_evidence",
                             "stage_target", allowed_zones=sorted(zones)))
    for name in sorted(spec["data"]["protected_sets"]):
        result.append(ob(f"data.{name}", "final_content_comparison",
                         "verify_final_state",
                         comparison_contract=spec["data"]["comparison_contract_ref"]))
    for invariant in sorted(spec["business"]["invariants"], key=lambda i: i["id"]):
        result.append(ob(f"business.{invariant['id']}", "business_invariant",
                         "verify_final_state"))
    for control in sorted(spec["security"]["required_controls"]):
        result.append(ob(f"security.{control}", "target_control_test",
                         "record_activation_intent"))
    for effect in sorted(spec["business"]["external_effects"], key=lambda e: e["id"]):
        result.append(ob(f"effect.{effect['id']}", "external_result_lookup",
                         "pause_admission"))
    availability = spec["availability"]
    result.append(ob("availability.write_admission_pause", "measured_budget",
                     "stabilize",
                     budget_ms=availability["max_write_admission_pause_ms"]))
    result.append(ob("availability.client_completion_wait", "measured_budget",
                     "stabilize",
                     budget_ms=availability["max_client_completion_wait_ms"]))
    if spec["operations"]["restore_test_required"]:
        result.append(ob("operations.restore_test", "restore_drill", "stabilize"))
    return sorted(result, key=lambda o: o["id"])


_DATA_KINDS = {ResourceKind.DATABASE, ResourceKind.STORAGE, ResourceKind.QUEUE}


def _sets(step, members, resources):
    """Symbolic read/write sets over the unit's members (§12.5 conflict input)."""
    data = sorted(m for m in members if resources[m] in _DATA_KINDS)
    authority = ["authority/unit"]
    source_data = [f"source/{m}" for m in data]
    target_data = [f"target/{m}" for m in data]
    target_all = [f"target/{m}" for m in members]
    table = {
        "stage_target": ([], target_all),
        "start_sync": (source_data, target_data),
        "check_cutover_ready": (authority + source_data + target_data, []),
        "pause_admission": (authority, [f"source/{m}/admission" for m in members]),
        "fence_source_writes": (authority, [f"source/{m}/write-policy" for m in data]),
        "copy_content": (source_data, target_data),
        "apply_final_changes": (source_data, target_data),
        "verify_final_state": (source_data + target_data, []),
        "record_activation_intent": (authority, authority),
        "activate_target": (authority, authority + [f"{t}/write-policy" for t in target_data]),
        "commit_route": (authority, ["route/unit"]),
        "stabilize": (authority + target_all, []),
    }
    reads, writes = table[step.step_id]
    return sorted(reads), sorted(writes)


def compile_revision(
    contract,
    snapshot,
    *,
    requested_nodes,
    expected_tenant,
    source,
    target,
    manifest,
    qualification,
    rows,
    authenticate,
    clock,
    required_grade=EvidenceGrade.LIVE,
):
    if not isinstance(contract, ServiceContract) or not isinstance(
        snapshot, DependencySnapshot
    ):
        raise ContractError("Typed contract and dependency snapshot required")
    if not all(isinstance(p, EnvironmentProfile) for p in (source, target)):
        raise ContractError("Typed environment profiles required")
    if not isinstance(manifest, CapabilityManifest) or not callable(clock):
        raise ContractError("Typed manifest and trusted clock required")
    contract.inspect(expected_tenant=expected_tenant)
    started = utc(clock())
    spec = contract.document["spec"]
    blockers = []
    unsupported = remediation = False

    # C0: an unregistered contract is only a normalized document.
    registered = False
    if callable(authenticate):
        try:
            registered = authenticate(contract) is True
        except Exception:
            registered = False
    if not registered:
        blockers.append(_blocker("C0", "CONTRACT_REGISTRATION_REQUIRED"))

    if contract.profile != MigrationProfile.SINGLE_AUTHORITY:
        unsupported = True
        blockers.append(_blocker("C3", "PROFILE_NOT_COMPILED",
                                 profile=str(contract.profile)))

    # C1-C2: evidence-bound boundary, recomputed here.
    boundary = analyze_boundary(
        contract, snapshot, requested_nodes=requested_nodes,
        expected_tenant=expected_tenant, authenticate=authenticate, clock=clock,
    )
    if boundary["status"] == "MERGE_REQUIRED" and not boundary["blockers"]:
        remediation = True
        blockers.append(_blocker("C2", "BOUNDARY_MERGE_REQUIRED",
                                 additional_members=boundary["additional_members"],
                                 reasons=boundary["coupling_reasons"]))
    for code in boundary["blockers"]:
        blockers.append(_blocker("C2", code))

    # C3: directional capability for exactly this template's operations.
    operations = required_operations(manifest.strategy_id)
    assessment = None
    if operations is None:
        unsupported = True
        blockers.append(_blocker("C3", "STRATEGY_TEMPLATE_UNKNOWN",
                                 strategy=manifest.strategy_id))
    else:
        missing = sorted(set(operations) - set(manifest.operations))
        if missing:
            unsupported = True
            blockers.append(_blocker("C3", "CAPABILITY_OPERATION_UNSUPPORTED",
                                     missing_operations=missing))
        assessment = assess_strategy(
            contract, expected_tenant=expected_tenant, source=source, target=target,
            manifest=manifest, qualification=qualification,
            required_operations=operations, rows=rows, authenticate=authenticate,
            clock=clock, required_grade=required_grade,
        )
        if assessment["status"] == "UNSUPPORTED":
            unsupported = True
        if assessment["status"] != "QUALIFIED_CANDIDATE" and not missing:
            blockers.append(_blocker("C3", assessment["reason"]))

    # C4: decidable constraints. Unreconcilable external effects cannot be
    # executed unattended (§14.3, §17.4); they need a remediation first.
    for effect in spec["business"]["external_effects"]:
        if not _reconcilable(effect):
            remediation = True
            blockers.append(_blocker("C4", "EXTERNAL_EFFECT_NOT_RECONCILABLE",
                                     effect=effect["id"]))

    finished = utc(clock())
    if finished < started:
        blockers.append(_blocker("C11", "CLOCK_REGRESSION"))
    inputs = {
        "contract_digest": contract.fingerprint,
        "snapshot_digest": snapshot.fingerprint,
        "unit_digest": boundary["unit_digest"],
        "manifest_digest": manifest.fingerprint,
        "qualification_digest": qualification.fingerprint
        if isinstance(qualification, StrategyQualification) else None,
        "source_profile_digest": source.fingerprint,
        "target_profile_digest": target.fingerprint,
    }
    if unsupported:
        status = "UNSUPPORTED"
    elif remediation:
        status = "REQUIRES_REMEDIATION"
    elif blockers:
        status = "NEEDS_EVIDENCE"
    else:
        status = "COMPILED"
    plan = None
    if status == "COMPILED":
        plan = _freeze(contract, snapshot, boundary, manifest, qualification,
                       source, target, inputs, spec)
    return plan, {
        "schema_version": 1,
        "compiler_version": COMPILER_VERSION,
        "status": status,
        "blockers": sorted(blockers, key=lambda b: (b["stage"], b["code"])),
        "inputs": inputs,
        "plan_digest": plan.digest if plan else None,
        "plan": json.loads(plan.canonical) if plan else None,
        "approval_available": plan is not None,
        "executable": False,
        "authorizes_execution": False,
        "evaluated_at": finished.isoformat(),
    }


def _freeze(contract, snapshot, boundary, manifest, qualification, source, target,
            inputs, spec):
    members = tuple(boundary["members"])
    resources = {r.node_id: r.kind for r in snapshot.graph.resources}
    scopes = {r.node_id: r.scope.environment_id for r in snapshot.graph.resources}
    uids = dict(snapshot.resource_uids)
    template = TEMPLATES[manifest.strategy_id]
    prefix = boundary["unit_digest"][:16]
    ids = {s.step_id: f"{prefix}.{i:02d}.{s.step_id}" for i, s in enumerate(template)}
    obligations = _obligations(spec, template)
    evidence_by_step = {}
    for o in obligations:
        evidence_by_step.setdefault(o["required_at"], []).append(o["id"])
    operations, previous = [], None
    for step in template:
        reads, writes = _sets(step, members, resources)
        operations.append({
            "operation_id": ids[step.step_id],
            "kind": step.step_id,
            "phase": step.phase,
            "adapter": None if step.operation is None else {
                "adapter_id": manifest.adapter_id,
                "adapter_version": manifest.adapter_version,
                "adapter_digest": manifest.adapter_digest,
                "manifest_digest": manifest.fingerprint,
                "operation": step.operation,
            },
            "read_set": reads,
            "write_set": writes,
            "requires": sorted(step.requires),
            "ensures": sorted(step.ensures),
            "depends_on": [] if previous is None else [previous],
            "idempotency": {"key_scope": "tenant+execution+operation",
                            "same_key_different_input": "reject"},
            "on_uncertain": "observe_or_safe_hold",
            "recovery_boundary": step.boundary,
            "required_evidence": sorted(evidence_by_step.get(step.step_id, [])),
        })
        previous = ids[step.step_id]
    for o in obligations:
        o["required_at"] = ids[o["required_at"]]
    valid_until = min(qualification.expires_at, snapshot.graph.valid_until)
    document = {
        "schema_version": 1,
        "kind": "MigrationPlanRevision",
        "compiler_version": COMPILER_VERSION,
        "tenant_id": contract.tenant_id,
        "service_id": contract.service_id,
        "profile": str(contract.profile),
        "inputs": inputs,
        "unit": {"members": list(members), "unit_digest": boundary["unit_digest"]},
        "environments": {"source": source.profile_id, "target": target.profile_id},
        "resources": {
            node: {
                "kind": str(resources[node]),
                "environment": scopes[node],
                "resource_uid": uids[node],
            }
            for node in members
        },
        "strategy": {"strategy_id": manifest.strategy_id,
                     "strategy_version": manifest.strategy_version,
                     "evidence_grade": str(qualification.grade)},
        "operations": operations,
        "obligations": obligations,
        "recovery": {
            PRE: "ABORT_ONLY_AFTER_TARGET_INACTIVE_VERIFIED_THEN_RESTORE_SOURCE_ADMISSION",
            POST: "SAFE_HOLD_ON_UNCERTAINTY_FAILBACK_REQUIRES_SEPARATE_PLAN",
            "source_auto_reopen_after_possible_activation": "forbidden",
            "source_retirement": "separate_approval_required",
            "cleanup": "owned_uid_scoped_temporary_resources_only",
        },
        "approval_policy": {
            "scope": "processing_authority_handoff",
            "separation_of_duties": spec["approval"]["separation_of_duties"],
            "high_risk_handoff": spec["approval"]["high_risk_handoff"],
        },
        "deferred": {
            "lowering": "C9_NOT_LOWERED",
            "exit_contract": {"remove": sorted(spec["exit"]["remove"]),
                              "allowed_remaining": sorted(spec["exit"]["allowed_remaining"])},
            "remaining_strategy_decisions": boundary["remaining_strategy_decisions"],
        },
        "valid_until": utc(valid_until).isoformat(),
    }
    _check_consistency(document, members)
    return PlanRevision(canonical_json(document, limit=MAX_PLAN_BYTES))


def _check_consistency(document, members):
    """C10 internal invariants; a violation is a compiler defect, not a status."""
    ops = document["operations"]
    ids = [o["operation_id"] for o in ops]
    if len(set(ids)) != len(ids):
        raise ContractError("Compiler consistency violation")
    seen = set()
    for op in ops:
        if any(d not in seen for d in op["depends_on"]):
            raise ContractError("Compiler consistency violation")
        seen.add(op["operation_id"])
        for ref in op["read_set"] + op["write_set"]:
            node = ref.split("/")[1] if ref.count("/") >= 1 else None
            if ref.startswith(("source/", "target/")) and node not in members:
                raise ContractError("Compiler consistency violation")
    if any(o["required_at"] not in seen for o in document["obligations"]):
        raise ContractError("Compiler consistency violation")
    boundaries = [o["recovery_boundary"] for o in ops]
    first_post = boundaries.index(POST)
    if any(b != POST for b in boundaries[first_post:]) or ops[first_post]["kind"] != (
        "record_activation_intent"
    ):
        raise ContractError("Compiler consistency violation")
    if not isinstance(document["valid_until"], str):
        raise ContractError("Compiler consistency violation")


def compile_plan(contract, snapshot, **kwargs):
    """JSON-ready result only; use ``compile_revision`` for the frozen object."""
    return compile_revision(contract, snapshot, **kwargs)[1]
