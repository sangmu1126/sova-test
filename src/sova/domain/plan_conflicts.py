"""§12.5 cross-plan scope conflicts over physical identities. Not a lock service.

Symbolic read/write sets in a frozen plan are resolved to absolute keys:
source resources by (environment, resource UID), target placements by
(target profile, tenant, service, node), unit authority per member source
resource, and the service route. Two plans conflict when one writes a key the
other reads or writes. A reference that cannot be resolved is treated as a
conflict, never as "no overlap". The deterministic lock order lets a lock
service acquire claims without deadlock; holding locks is a separate concern.
"""

import json

from sova.domain.contracts import ContractError
from sova.domain.plan_compiler import PlanRevision

READ, WRITE = "read", "write"


def _document(revision):
    if not isinstance(revision, PlanRevision):
        raise ContractError("Frozen compiler plan required")
    return json.loads(revision.canonical)


def _resolve(ref, plan):
    """Map one symbolic ref to absolute keys, or None when it cannot be resolved."""
    parts = ref.split("/")
    resources = plan["resources"]
    tenant, service = plan["tenant_id"], plan["service_id"]
    if parts[0] in ("source", "target") and len(parts) >= 2:
        node = parts[1]
        if node not in resources:
            return None
        if parts[0] == "source":
            item = resources[node]
            return [("source", item["environment"], item["resource_uid"])]
        return [("target", plan["environments"]["target"], tenant, service, node)]
    if ref == "authority/unit":
        return [
            ("authority", item["environment"], item["resource_uid"])
            for item in resources.values()
        ]
    if ref == "route/unit":
        return [("route", tenant, service)]
    return None


def lock_set(revision):
    """Strongest mode per absolute key, in a fixed global acquisition order."""
    plan = _document(revision)
    modes = {}
    unresolved = []
    for op in plan["operations"]:
        for mode, refs in ((READ, op["read_set"]), (WRITE, op["write_set"])):
            for ref in refs:
                keys = _resolve(ref, plan)
                if keys is None:
                    unresolved.append(ref)
                    continue
                for key in keys:
                    if modes.get(key) != WRITE:
                        modes[key] = mode
    return tuple(sorted(modes.items())), tuple(sorted(set(unresolved)))


def detect_conflicts(candidate, active):
    """Compare a candidate plan with currently active plans.

    Returns ``{"status": "CONFLICT"|"NO_CONFLICT", "conflicts": [...]}``. The
    caller must evaluate this inside the same transaction that records the
    candidate's claims; a separate read-then-write would race.
    """
    if type(active) is not tuple:
        raise ContractError("Immutable active plan set required")
    mine, mine_unresolved = lock_set(candidate)
    conflicts = []
    if mine_unresolved:
        conflicts.append({"code": "SCOPE_UNRESOLVED", "refs": list(mine_unresolved)})
    mine = dict(mine)
    for other in active:
        if not isinstance(other, PlanRevision):
            raise ContractError("Frozen active plans required")
        if other.digest == candidate.digest:
            conflicts.append({"code": "SAME_PLAN_ALREADY_ACTIVE", "with_plan": other.digest})
            continue
        theirs, theirs_unresolved = lock_set(other)
        if theirs_unresolved:
            conflicts.append(
                {"code": "ACTIVE_SCOPE_UNRESOLVED", "with_plan": other.digest}
            )
            continue
        for key, mode in theirs:
            if key in mine and WRITE in (mode, mine[key]):
                conflicts.append(
                    {
                        "code": "OVERLAPPING_SCOPE",
                        "with_plan": other.digest,
                        "key": list(key),
                        "modes": sorted((mine[key], mode)),
                    }
                )
    return {
        "status": "CONFLICT" if conflicts else "NO_CONFLICT",
        "conflicts": conflicts,
        "lock_order": [list(k) + [m] for k, m in sorted(mine.items())],
        "executable": False,
    }
