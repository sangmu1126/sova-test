"""C9 lowering to the existing LifecyclePlan and the C12 approval recheck.

The frozen plan is lowered deterministically into the existing approval and
command contract (``sova_security.contracts.plans``), so the existing approval
rules (exact digest, purpose, author is not approver, expiry) and command
issuance stay the single authority. Nothing here approves or executes.

``verify_approved_plan`` is the execution-time check: it recompiles from the
current authenticated inputs, lowers again, and requires byte-equal plans
before the existing ``PlanApproval.require_valid``. An approved plan whose
inputs changed, expired, or lost authentication is refused (AT-10/11/13).
"""

from datetime import datetime
import json

from sova.domain.contracts import ContractError, ResourceScope, canonical_json, identifier
from sova.domain.evidence import Gate, RevisionSet, VerificationObligation
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plans import LifecyclePlan, PlanApproval, PlanPurpose, PlanStep


PLATFORM_ADAPTER = "sova.control-plane"
PLATFORM_ENVIRONMENT = "sova-control-plane"
EDGE_ENVIRONMENT = "edge"

# Which side of the handoff each step acts on. Copy/verify act on the target
# while reading the source; their full read/write sets travel in parameters.
_SIDE = {
    "stage_target": "target",
    "start_sync": "target",
    "check_cutover_ready": "platform",
    "pause_admission": "source",
    "fence_source_writes": "source",
    "copy_content": "target",
    "apply_final_changes": "target",
    "verify_final_state": "target",
    "record_activation_intent": "platform",
    "activate_target": "target",
    "commit_route": "edge",
    "stabilize": "platform",
}

# Existing verification gates; the plan's step binding travels in parameters.
_GATES = {
    "placement_evidence": Gate.REQUIREMENTS,
    "independent_write_denial_probe": Gate.PREFLIGHT,
    "external_result_lookup": Gate.PREFLIGHT,
    "final_content_comparison": Gate.DATA,
    "business_invariant": Gate.BEHAVIOR,
    "target_control_test": Gate.PROTECTION,
    "authority_readback": Gate.POSTFLIGHT,
    "stale_route_probe": Gate.POSTFLIGHT,
    "measured_budget": Gate.POSTFLIGHT,
    "restore_drill": Gate.POSTFLIGHT,
}


def _document(revision):
    if not isinstance(revision, PlanRevision):
        raise ContractError("Frozen compiler plan required")
    return json.loads(revision.canonical)


def lower_plan(revision, *, created_by, verifiers):
    """Deterministic C9. ``verifiers`` names the verifier for every obligation kind.

    There is no default verifier: evidence must match a named, registered
    verifier version, so a missing mapping is an error, not a fallback.
    """
    plan = _document(revision)
    identifier(created_by, "plan author")
    if type(verifiers) is not dict:
        raise ContractError("Explicit verifier mapping required")
    kinds = {o["kind"] for o in plan["obligations"]}
    if set(verifiers) != kinds:
        raise ContractError("Every obligation kind needs exactly one named verifier")
    for version in verifiers.values():
        identifier(version, "verifier version")
    tenant, app = plan["tenant_id"], plan["service_id"]
    unit = "unit-" + plan["unit"]["unit_digest"][:24]
    environment = {
        "source": plan["environments"]["source"],
        "target": plan["environments"]["target"],
        "platform": PLATFORM_ENVIRONMENT,
        "edge": EDGE_ENVIRONMENT,
    }
    steps, previous = [], None
    for op in plan["operations"]:
        side = _SIDE[op["kind"]]
        adapter = op["adapter"]
        parameters = {
            "compiled_plan_digest": revision.digest,
            "operation_id": op["operation_id"],
            "phase": op["phase"],
            "adapter_operation": None if adapter is None else adapter["operation"],
            "manifest_digest": None if adapter is None else adapter["manifest_digest"],
            "read_set": op["read_set"],
            "write_set": op["write_set"],
            "requires": op["requires"],
            "ensures": op["ensures"],
            "required_evidence": op["required_evidence"],
            "idempotency": op["idempotency"],
            "on_uncertain": op["on_uncertain"],
            "recovery_boundary": op["recovery_boundary"],
        }
        steps.append(
            PlanStep(
                op["operation_id"],
                ResourceScope(tenant, app, environment[side], unit),
                PLATFORM_ADAPTER if adapter is None else adapter["adapter_id"],
                op["kind"],
                canonical_json(parameters),
                plan["inputs"]["snapshot_digest"],
                () if previous is None else (previous,),
            )
        )
        previous = op["operation_id"]
    step_side = {op["operation_id"]: _SIDE[op["kind"]] for op in plan["operations"]}
    obligations = tuple(
        VerificationObligation(
            o["id"],
            ResourceScope(tenant, app, environment[step_side[o["required_at"]]], unit),
            _GATES[o["kind"]],
            verifiers[o["kind"]],
        )
        for o in plan["obligations"]
    )
    inputs = plan["inputs"]
    return LifecyclePlan(
        "handoff-" + revision.digest[:24],
        tenant,
        app,
        created_by,
        PlanPurpose.MIGRATION_PHASE,
        RevisionSet(
            inputs["contract_digest"],
            inputs["manifest_digest"],
            inputs["qualification_digest"],
            inputs["snapshot_digest"],
        ),
        datetime.fromisoformat(plan["valid_until"]),
        tuple(steps),
        obligations,
        origin_plan_digest=revision.digest,
        reason="Compiled planned single-authority handoff; separate approval required",
    )


def verify_approved_plan(approved, approval, *, recompile, verifiers, now):
    """C12 recheck before any command is issued from an approved plan.

    ``recompile()`` must run ``compile_revision`` with the current authenticated
    inputs and return its ``(revision, result)`` pair.
    """
    if not isinstance(approved, LifecyclePlan) or not isinstance(approval, PlanApproval):
        raise ContractError("Typed approved plan and approval required")
    if not callable(recompile):
        raise ContractError("Recompilation callback required")
    revision, result = recompile()
    if result.get("status") != "COMPILED" or revision is None:
        raise ContractError("Approved plan inputs are no longer compilable")
    if result.get("plan_digest") != revision.digest:
        raise ContractError("Compiler result does not match its frozen plan")
    current = lower_plan(revision, created_by=approved.created_by, verifiers=verifiers)
    if current != approved or current.fingerprint != approved.fingerprint:
        raise ContractError("Approved plan differs from the current compiled plan")
    approval.require_valid(approved, now)
    return approved
