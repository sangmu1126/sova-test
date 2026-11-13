"""Independence after a completed handoff (MIG-SPEC §20, Part II §9.2).

Handoff completion and independence are separate claims. Completion means the
target took business authority under the plan's gates; independence means the
service still works when what the exit contract removes is actually blocked.
This module derives the independence drills a frozen plan's exit contract
requires and decides each independence level from drill verdicts:

* every dependency the contract removes gets a blocked cold-start drill;
* compute, restart, operations, and platform-exit drills are always required;
* a migrated identity adds the source-IdP-blocked drill (IAM-08) and its
  retirement checks (IAM-10, IAM-11); a retained IdP is reported, never hidden.

A level passes only when all its drills pass on current evidence. Missing,
stale, or change-invalidated evidence is UNKNOWN, never PASS. Dependencies the
contract allows to remain are always listed. Retiring the source stays a
separate approval (INV-10); a passing assessment only makes it reviewable.
"""

from dataclasses import dataclass
from enum import StrEnum
import json

from sova.domain.contracts import ContractError, identifier
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import AuthorityRecord, HandoffState
from sova.domain.plan_compiler import PlanRevision

EXIT_DRILL_KIND = "exit_drill"


class IndependenceLevel(StrEnum):
    COMPUTE_INDEPENDENT = "COMPUTE_INDEPENDENT"
    DATA_IDENTITY_INDEPENDENT = "DATA_IDENTITY_INDEPENDENT"
    RESTART_VERIFIED = "RESTART_VERIFIED"
    OPERATIONS_VERIFIED = "OPERATIONS_VERIFIED"
    PROVIDER_EXIT_VERIFIED = "PROVIDER_EXIT_VERIFIED"
    PLATFORM_EXIT_VERIFIED = "PLATFORM_EXIT_VERIFIED"


_PROVIDER_PARTS = (
    IndependenceLevel.COMPUTE_INDEPENDENT,
    IndependenceLevel.DATA_IDENTITY_INDEPENDENT,
    IndependenceLevel.RESTART_VERIFIED,
    IndependenceLevel.OPERATIONS_VERIFIED,
)


@dataclass(frozen=True, slots=True)
class ExitDrill:
    drill_id: str
    level: IndependenceLevel | None  # None: a retirement check, not a level
    purpose: str


def exit_drills(revision):
    """The independence drills a frozen plan's exit contract requires."""
    if not isinstance(revision, PlanRevision):
        raise ContractError("Frozen compiled plan required")
    exit_contract = json.loads(revision.canonical)["deferred"]["exit_contract"]
    level = IndependenceLevel
    drills = [
        ExitDrill("exit.compute.source_compute_blocked", level.COMPUTE_INDEPENDENT,
                  "a new target instance serves business with source compute unreachable"),
        ExitDrill("exit.restart.cold_start_fresh_credentials", level.RESTART_VERIFIED,
                  "new instance, connections, tokens and secrets obtained without cached state"),
        ExitDrill("exit.operations.batch_backup_restore_audit_deploy",
                  level.OPERATIONS_VERIFIED,
                  "batch, backup, restore, audit and a new deployment run on the target"),
        ExitDrill("exit.platform.customer_operates_after_revocation",
                  level.PLATFORM_EXIT_VERIFIED,
                  "platform access revoked; customer deploys, rotates secrets and restores"),
    ]
    for dependency in exit_contract["remove"]:
        identifier(dependency, "exit dependency")
        drills.append(ExitDrill(
            f"exit.removed.{dependency}.blocked_cold_start", level.DATA_IDENTITY_INDEPENDENT,
            f"with {dependency} blocked, a new instance starts and completes new business"))
    identity = exit_contract.get("identity")
    if identity is not None and identity["strategy"] == "MIGRATE_IDENTITY":
        for item in identity["independence_obligations"]:
            is_cold_start = item["id"].startswith("IAM-08.")
            drills.append(ExitDrill(
                item["id"], level.DATA_IDENTITY_INDEPENDENT if is_cold_start else None,
                "identity independence drill (Part II §9.1)"))
    ids = [d.drill_id for d in drills]
    if len(ids) != len(set(ids)):
        raise ContractError("Duplicate exit drill")
    return tuple(drills)


@dataclass(frozen=True, slots=True)
class DrillVerdict:
    result: VerificationResult
    evidence_id: str | None
    reason: str


def assess_exit(revision, record, verdicts):
    """Independence levels for a unit from per-drill verdicts on current evidence."""
    if not isinstance(record, AuthorityRecord) or record.compiled_digest != revision.digest:
        raise ContractError("Authority record for this compiled plan required")
    drills = exit_drills(revision)
    if type(verdicts) is not dict or set(verdicts) != {d.drill_id for d in drills} or any(
        not isinstance(v, DrillVerdict) for v in verdicts.values()
    ):
        raise ContractError("A verdict for exactly every exit drill is required")
    exit_contract = json.loads(revision.canonical)["deferred"]["exit_contract"]
    identity = exit_contract.get("identity")
    retained = set(exit_contract["allowed_remaining"])
    if identity is not None:
        retained |= set(identity["retained_dependencies"])
    complete = record.state == HandoffState.HANDOFF_COMPLETE

    def status(members):
        if not complete:
            return "NOT_STARTED"
        if not members:
            return "NOT_REQUIRED"
        results = {verdicts[d.drill_id].result for d in members}
        if VerificationResult.FAIL in results:
            return "FAIL"
        return "PASS" if results == {VerificationResult.PASS} else "UNKNOWN"

    levels = {}
    for level in IndependenceLevel:
        if level == IndependenceLevel.PROVIDER_EXIT_VERIFIED:
            continue
        members = [d for d in drills if d.level == level]
        levels[str(level)] = {
            "status": status(members),
            "drills": [{"id": d.drill_id, "purpose": d.purpose,
                        "result": str(verdicts[d.drill_id].result),
                        "evidence_id": verdicts[d.drill_id].evidence_id,
                        "reason": verdicts[d.drill_id].reason} for d in members],
        }
    parts = [levels[str(p)]["status"] for p in _PROVIDER_PARTS]
    provider = ("NOT_STARTED" if not complete else "FAIL" if "FAIL" in parts
                else "PASS" if set(parts) <= {"PASS", "NOT_REQUIRED"} else "UNKNOWN")
    levels[str(IndependenceLevel.PROVIDER_EXIT_VERIFIED)] = {
        "status": provider, "derived_from": [str(p) for p in _PROVIDER_PARTS]}

    identity_status = _identity(identity, verdicts, complete)
    return {
        "schema_version": 1,
        "plan_digest": record.plan_digest,
        "compiled_digest": revision.digest,
        "unit_digest": record.unit_digest,
        "handoff_state": str(record.state),
        "levels": levels,
        "identity": identity_status,
        "residual_dependencies": sorted(retained),
        "source_retirement": {
            "approval": "SEPARATE_APPROVAL_REQUIRED",
            "reviewable": provider == "PASS"
            and identity_status["status"] not in ("IDENTITY_BLOCKED", "IDENTITY_UNKNOWN"),
        },
    }


def _identity(identity, verdicts, complete):
    if identity is None:
        return {"status": "NOT_APPLICABLE"}
    if identity["strategy"] == "RETAIN_SOURCE_IDP":
        return {"status": "SOURCE_IDP_RETAINED",
                "retained": list(identity["retained_dependencies"])}
    if not complete:
        return {"status": "IDENTITY_CUTOVER_IN_PROGRESS"}

    def result(prefix):
        found = [v for k, v in verdicts.items() if k.startswith(prefix)]
        return found[0] if found else None

    cold = result("IAM-08.")
    if cold is None:
        status = "IDENTITY_VALIDATED"  # the contract does not ask for the blocked drill
    elif cold.result == VerificationResult.PASS:
        status = "SOURCE_IDENTITY_INDEPENDENT"
    elif cold.result == VerificationResult.FAIL:
        status = "IDENTITY_BLOCKED"
    elif cold.reason == "missing_bound_evidence":
        status = "IDENTITY_VALIDATED"  # gate passed in the handoff, drill not yet run
    else:
        status = "IDENTITY_UNKNOWN"

    def retirement(prefix):
        verdict = result(prefix)
        return "NOT_REQUIRED" if verdict is None else str(verdict.result)

    return {
        "status": status,
        "old_issuer_rejected_after_end": retirement("IAM-10."),
        "bridge_and_dual_issuer_retired": retirement("IAM-11."),
    }
