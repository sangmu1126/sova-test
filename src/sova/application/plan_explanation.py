"""Explain a frozen compiled plan for review before approval (§24.2, §27.4).

The input is the exact canonical text whose SHA-256 is the plan digest. It is
refused unless it is already canonical and, when given, matches the digest
the approver was asked to approve. The explanation is derived from the frozen
plan only: steps with their side, recovery boundary, adapter operation and
required evidence; verification gates; absolute claim keys; independence
drills; and failback linkage. It authorizes nothing.
"""

import json

from sova.domain.contracts import ContractError, canonical_json, digest
from sova.domain.exit_readiness import exit_drills
from sova.domain.plan_compiler import MAX_PLAN_BYTES, PlanRevision
from sova.domain.plan_conflicts import lock_set
from sova.domain.plan_lowering import gate_for, side_for



def load_canonical_plan(text, *, expect_digest=None):
    if type(text) is not str:
        raise ContractError("Canonical plan text required")
    text = text[:-1] if text.endswith("\n") else text
    if not 2 <= len(text.encode()) <= MAX_PLAN_BYTES:
        raise ContractError("Bounded canonical plan text required")
    document = json.loads(text)
    if canonical_json(document, limit=MAX_PLAN_BYTES) != text:
        raise ContractError("Plan text is not in canonical form")
    if document.get("kind") != "MigrationPlanRevision":
        raise ContractError("Not a compiled migration plan")
    revision = PlanRevision(text)
    if expect_digest is not None and revision.digest != digest(expect_digest):
        raise ContractError("Plan digest differs from the one under review")
    return revision


def explain_plan(revision):
    if not isinstance(revision, PlanRevision):
        raise ContractError("Frozen compiled plan required")
    doc = json.loads(revision.canonical)
    by_step = {}
    for o in doc["obligations"]:
        by_step.setdefault(o["required_at"], []).append(
            {"id": o["id"], "kind": o["kind"], "gate": gate_for(o["kind"]).name})
    steps = []
    for n, op in enumerate(doc["operations"]):
        steps.append({
            "n": n,
            "kind": op["kind"],
            "reaches": op["phase"],
            "acts_on": side_for(op["kind"]),
            "adapter_operation": None if op["adapter"] is None else op["adapter"]["operation"],
            "recovery_boundary": op["recovery_boundary"],
            "requires": op["requires"],
            "ensures": op["ensures"],
            "evidence": sorted(by_step.get(op["operation_id"], []), key=lambda e: e["id"]),
        })
    locks, unresolved = lock_set(revision)
    failback = doc.get("failback_of")
    return {
        "schema_version": 1,
        "compiled_digest": revision.digest,
        "tenant_id": doc["tenant_id"],
        "service_id": doc["service_id"],
        "direction": doc["environments"],
        "unit": {"members": doc["unit"]["members"], "resources": doc["resources"]},
        "strategy": doc["strategy"],
        "valid_until": doc["valid_until"],
        "steps": steps,
        "point_of_no_return": next(
            (s["kind"] for s in steps if s["recovery_boundary"] != steps[0][
                "recovery_boundary"]), None),
        "claims": [{"key": list(k), "mode": m} for k, m in locks],
        "unresolved_scope": list(unresolved),
        "independence_drills": [
            {"id": d.drill_id, "level": None if d.level is None else str(d.level),
             "purpose": d.purpose} for d in exit_drills(revision)],
        "controllers": doc.get("controllers"),
        "recovery": doc["recovery"],
        "approval_policy": doc["approval_policy"],
        "failback_of": None if failback is None else {
            "forward_compiled_digest": failback["forward_compiled_digest"],
            "forward_epoch": failback["forward_epoch"],
            "reason": failback["reason"],
            "return_resources": failback["return_resources"],
        },
        "authorizes_execution": False,
    }
