"""Candidate strategy search over a fixed catalog (MIG-SPEC §12.3, §12.4).

Every candidate (a capability manifest with its qualification) is compiled
with the same authenticated inputs. Mandatory conditions are never traded
against cost: a candidate is feasible (COMPILED), not satisfied
(REQUIRES_REMEDIATION or UNSUPPORTED), or lacking evidence (NEEDS_EVIDENCE).
Only feasible candidates are ranked, by one explainable rule taken from the
write-pause budget (§16.4): a plan that copies the whole data set inside the
fenced window pauses writes longer than one that applies final changes
only. The approver still chooses and approves one exact plan digest.

``NO_FEASIBLE_PLAN`` means none of the candidates given here qualifies; it
never claims that no technical way exists. ``SEARCH_INCOMPLETE`` means some
candidate could not be decided (missing evidence, invalid input, or the
search budget), so the absence of a feasible plan is not concluded.
"""

import json

from sova.domain.contracts import ContractError
from sova.domain.plan_compiler import compile_revision

MAX_CANDIDATES = 16
_FULL_COPY = "copy_content"


def _fenced_full_copy(revision):
    ops = json.loads(revision.canonical)["operations"]
    kinds = [op["kind"] for op in ops]
    fence = kinds.index("fence_source_writes")
    activate = kinds.index("activate_target")
    return _FULL_COPY in kinds[fence:activate]


def search_plans(contract, snapshot, candidates, **compile_inputs):
    """Compile each (manifest, qualification) candidate; never relax a requirement."""
    if type(candidates) is not tuple or not candidates:
        raise ContractError("Non-empty immutable candidate tuple required")
    for key in ("manifest", "qualification", "failback"):
        if key in compile_inputs:
            raise ContractError("Candidates carry their own manifest and qualification")
    evaluated, revisions, undecided = [], {}, len(candidates) > MAX_CANDIDATES
    for manifest, qualification in candidates[:MAX_CANDIDATES]:
        entry = {"strategy_id": getattr(manifest, "strategy_id", None),
                 "manifest_digest": getattr(manifest, "fingerprint", None)}
        try:
            revision, result = compile_revision(contract, snapshot, manifest=manifest,
                                                qualification=qualification,
                                                **compile_inputs)
        except ContractError:
            entry.update(status="INPUT_INVALID", plan_digest=None, blockers=[])
            undecided = True
            evaluated.append(entry)
            continue
        entry.update(status=result["status"], blockers=result["blockers"],
                     plan_digest=None if revision is None else revision.digest)
        if revision is not None:
            revisions[revision.digest] = revision
            entry["full_copy_in_fenced_window"] = _fenced_full_copy(revision)
        if result["status"] == "NEEDS_EVIDENCE":
            undecided = True
        evaluated.append(entry)
    feasible = sorted(
        (e for e in evaluated if e["status"] == "COMPILED"),
        key=lambda e: (e["full_copy_in_fenced_window"], e["strategy_id"], e["plan_digest"]),
    )
    if feasible:
        status = "COMPILED"
    elif undecided:
        status = "SEARCH_INCOMPLETE"
    else:
        status = "NO_FEASIBLE_PLAN"
    return revisions, {
        "schema_version": 1,
        "status": status,
        "search_complete": not undecided,
        "evaluated": len(evaluated),
        "not_evaluated": max(0, len(candidates) - MAX_CANDIDATES),
        "candidates": evaluated,
        "feasible": [e["plan_digest"] for e in feasible],
        "recommended": feasible[0]["plan_digest"] if feasible else None,
        "recommendation_basis": "NO_FULL_COPY_INSIDE_FENCED_WINDOW_FIRST" if feasible
        else None,
        "requirements_relaxed": False,
        "approval_available": bool(feasible),
        "authorizes_execution": False,
    }
