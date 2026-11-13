"""Change impact over a frozen handoff plan (INV-08, MIG-SPEC §23.3).

A compiled plan and the evidence that satisfies its obligations describe the
world at the moment they were observed. This module indexes what each part of
a frozen plan depends on and decides, for a set of recorded change events:

* whether the plan's own inputs are stale (a unit resource, its schema, the
  contract, manifest, qualification, an environment profile, or an adapter
  changed after the plan's snapshot) - a stale plan must not issue new
  commands; re-evaluation needs a new snapshot and a new approval;
* which obligations' evidence is invalidated, and since when - evidence
  observed at or before the latest impacting change no longer counts;
* which conclusions are UNKNOWN because a change feed has a gap - an
  environment whose collector has not reported recently cannot be assumed
  unchanged.

When a change cannot be attributed precisely (no subject, unknown kind, or an
obligation kind this module does not know), the impact is widened rather than
dropped. Changes produced by this plan's own already-issued commands, inside
the declared write set of that command, are expected and ignored; an
attribution to a future step or outside the write set is treated as foreign.
Attribution and watermarks come from authorized collectors; this module does
not authenticate them.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
import json

from sova.domain.contracts import ContractError, digest, identifier, utc
from sova.domain.plan_compiler import PlanRevision
from sova.domain.plan_lowering import EDGE_ENVIRONMENT, PLATFORM_ENVIRONMENT
from sova.domain.plans import LifecyclePlan
from sova.domain.registry_attestation import REGISTRY_ENVIRONMENT

MAX_FEED_LAG = timedelta(minutes=15)


class ChangeKind(StrEnum):
    RESOURCE = "resource"  # configuration change or recreation of a resource
    SCHEMA = "schema"  # schema change of a data resource
    POLICY = "policy"  # permission, network, or security policy in an environment
    IDENTITY = "identity"  # IdP, OAuth client, JWKS, or user directory configuration
    CODE = "code"  # the service's own code or image
    VERIFIER = "verifier"  # implementation behind a named verifier version
    ADAPTER = "adapter"  # adapter implementation under a manifest
    CONTRACT = "contract"  # registry: service contract revision
    MANIFEST = "manifest"  # registry: capability manifest revision
    QUALIFICATION = "qualification"  # registry: strategy qualification revision
    ENVIRONMENT_PROFILE = "environment_profile"  # registry: profile revision
    UNCLASSIFIED = "unclassified"


_REGISTRY = {ChangeKind.CONTRACT, ChangeKind.MANIFEST, ChangeKind.QUALIFICATION,
             ChangeKind.ENVIRONMENT_PROFILE}

# Environments each verification kind actually observes, beyond the side of
# the step it is due at. Unknown kinds are treated as observing every side.
_OBSERVES = {
    "placement_evidence": {"target"},
    "independent_write_denial_probe": {"source"},
    "external_result_lookup": {"source", "target"},
    "final_content_comparison": {"source", "target"},
    "business_invariant": {"source", "target"},
    "target_control_test": {"target"},
    "authority_readback": {"source", "target"},
    "stale_route_probe": {"source", "edge"},
    "measured_budget": {"source", "target", "edge"},
    "restore_drill": {"target"},
    "return_target_check": {"target"},
    "identity_leak_scan": {"target", "platform"},
    "identity_mapping_check": {"source", "target"},
    "identity_flow_test": {"target", "edge"},
    "identity_operations_check": {"target"},
    # Independence drills run new instances against the whole unit (§20).
    "exit_drill": {"source", "target", "platform", "edge"},
}
_SIDES = frozenset({"source", "target", "platform", "edge"})


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    event_id: str
    tenant_id: str
    application_id: str
    kind: ChangeKind
    environment_id: str | None
    subject: str | None  # resource UID, adapter, profile, or verifier; None if unknown
    digest: str | None  # new artifact digest for registry kinds
    observed_at: datetime
    cause: tuple[str, str] | None = None  # (lifecycle plan digest, step id)

    def __post_init__(self):
        for field in ("event_id", "tenant_id", "application_id"):
            identifier(getattr(self, field), field)
        if not isinstance(self.kind, ChangeKind):
            raise ContractError("Typed change kind required")
        for field in ("environment_id", "subject"):
            if getattr(self, field) is not None:
                identifier(getattr(self, field), field)
        if self.digest is not None:
            digest(self.digest)
        if self.digest is not None and self.kind not in _REGISTRY:
            raise ContractError("Only registry changes carry an artifact digest")
        object.__setattr__(self, "observed_at", utc(self.observed_at))
        if self.cause is not None:
            if type(self.cause) is not tuple or len(self.cause) != 2:
                raise ContractError("Change cause is (plan digest, step id)")
            digest(self.cause[0])
            identifier(self.cause[1], "cause step")


@dataclass(frozen=True, slots=True)
class FeedWatermark:
    """A collector's claim that every change in ``environment_id`` up to
    ``observed_through`` has been recorded."""

    environment_id: str
    observed_through: datetime

    def __post_init__(self):
        identifier(self.environment_id, "environment_id")
        object.__setattr__(self, "observed_through", utc(self.observed_through))


@dataclass(frozen=True, slots=True)
class ImpactAssessment:
    plan_digest: str
    as_of: datetime
    plan_stale: bool
    plan_unknown: bool
    stale_since: tuple[tuple[str, datetime], ...]  # obligation -> latest impacting change
    unknown_obligations: frozenset
    reasons: tuple[str, ...]
    structure_as_of: datetime | None = None  # latest snapshot confirming the unit

    def require_commands_allowed(self):
        if not self.commands_allowed:
            raise PlanReevaluationRequired(self)
        return self

    def evidence_counts(self, obligation_id, observed_at):
        """True only if evidence observed at ``observed_at`` postdates every
        impacting change for the obligation and its feeds are complete."""
        if obligation_id in self.unknown_obligations:
            return False
        since = dict(self.stale_since).get(obligation_id)
        return since is None or utc(observed_at) > since

    @property
    def commands_allowed(self):
        return not (self.plan_stale or self.plan_unknown)

    @property
    def document(self):
        return {
            "plan_digest": self.plan_digest,
            "as_of": self.as_of.isoformat(),
            "plan_stale": self.plan_stale,
            "plan_unknown": self.plan_unknown,
            "commands_allowed": self.commands_allowed,
            "invalidated": {o: t.isoformat() for o, t in self.stale_since},
            "unknown": sorted(self.unknown_obligations),
            "reasons": list(self.reasons),
            "structure_as_of": None if self.structure_as_of is None
            else self.structure_as_of.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ImpactIndex:
    """What a frozen, lowered plan depends on."""

    plan_digest: str
    tenant_id: str
    application_id: str
    snapshot_observed_at: datetime
    side_of_environment: tuple[tuple[str, str], ...]
    required_feeds: frozenset
    members: tuple[tuple[str, str, str], ...]  # (environment, uid, node)
    inputs: tuple[tuple[str, str], ...]  # (kind:subject, digest)
    adapters: frozenset
    operations: tuple[tuple[str, str, frozenset, frozenset], ...]
    # (step, side, written source/target nodes, touched source/target nodes)
    obligations: tuple[tuple[str, frozenset, frozenset, str, str], ...]
    # (id, sides, touched source/target paths, kind, verifier version)

    @classmethod
    def from_plan(cls, revision, plan):
        if not isinstance(revision, PlanRevision) or not isinstance(plan, LifecyclePlan):
            raise ContractError("Frozen compiled plan and lowered plan required")
        if plan.origin_plan_digest != revision.digest:
            raise ContractError("Lowered plan does not come from this compiled plan")
        doc = json.loads(revision.canonical)
        source_envs = {r["environment"] for r in doc["resources"].values()}
        sides = {doc["environments"]["source"]: "source",
                 doc["environments"]["target"]: "target",
                 PLATFORM_ENVIRONMENT: "platform", EDGE_ENVIRONMENT: "edge"}
        sides.update({env: "source" for env in source_envs})
        feeds = frozenset(source_envs | {doc["environments"]["target"], PLATFORM_ENVIRONMENT,
                                         EDGE_ENVIRONMENT, REGISTRY_ENVIRONMENT})
        members = tuple(sorted((r["environment"], r["resource_uid"], node)
                               for node, r in doc["resources"].items()))
        i = doc["inputs"]
        inputs = (
            (f"{ChangeKind.CONTRACT}:{doc['service_id']}", i["contract_digest"]),
            (f"{ChangeKind.MANIFEST}:{doc['strategy']['strategy_id']}", i["manifest_digest"]),
            (f"{ChangeKind.QUALIFICATION}:{doc['strategy']['strategy_id']}",
             i["qualification_digest"]),
            (f"{ChangeKind.ENVIRONMENT_PROFILE}:{doc['environments']['source']}",
             i["source_profile_digest"]),
            (f"{ChangeKind.ENVIRONMENT_PROFILE}:{doc['environments']['target']}",
             i["target_profile_digest"]),
        )
        step_side = {s.step_id: sides.get(s.scope.environment_id) for s in plan.steps}
        operations, touched_by = [], {}
        for op in doc["operations"]:
            written = frozenset(_paths(op["write_set"]))
            touched = frozenset(_paths(op["read_set"] + op["write_set"]))
            touched_by[op["operation_id"]] = touched
            operations.append((op["operation_id"], step_side[op["operation_id"]], written,
                               touched))
        verifier = {o.obligation_id: o.verifier_version for o in plan.obligations}
        obligations = tuple(
            (o["id"], frozenset(_OBSERVES.get(o["kind"], _SIDES) | {step_side[o["required_at"]]}),
             touched_by[o["required_at"]], o["kind"], verifier[o["id"]])
            for o in doc["obligations"]
        )
        return cls(
            plan.fingerprint, doc["tenant_id"], doc["service_id"],
            datetime.fromisoformat(i["snapshot_observed_at"]),
            tuple(sorted(sides.items())), feeds, members, inputs,
            frozenset(op["adapter"]["adapter_id"] for op in doc["operations"] if op["adapter"]),
            tuple(operations), obligations,
        )


    def extended(self, obligations):
        """This index plus verification obligations outside the plan's steps, such
        as post-handoff independence drills, given as (id, kind, verifier version).
        They depend on every unit member and on the environments their kind observes."""
        if type(obligations) is not tuple:
            raise ContractError("Immutable extra obligations required")
        known = {o[0] for o in self.obligations}
        members = frozenset(("source", node) for _, _, node in self.members)
        extra = []
        for oid, kind, verifier in obligations:
            identifier(oid, "obligation_id")
            identifier(verifier, "verifier_version")
            if oid in known:
                raise ContractError("Duplicate obligation ID in impact index")
            known.add(oid)
            extra.append((oid, frozenset(_OBSERVES.get(kind, _SIDES)), members, kind, verifier))
        return replace(self, obligations=self.obligations + tuple(extra))


def _paths(paths):
    """Source/target node paths as (side, node); platform paths are not resources."""
    for path in paths:
        side, _, rest = path.partition("/")
        if side in ("source", "target") and rest:
            yield side, rest.split("/")[0]


class PlanReevaluationRequired(ContractError):
    """Changes since the plan's snapshot, or a change-feed gap, block progress."""

    def __init__(self, assessment):
        super().__init__("Plan must be re-evaluated: " + ", ".join(assessment.reasons))
        self.assessment = assessment


def assess_impact(index, events, watermarks, *, issued, now, max_feed_lag=MAX_FEED_LAG,
                  reaffirmed_at=None):
    """Decide the impact of recorded ``events`` on ``index``.

    ``issued`` is how many leading plan steps may already have had commands
    issued: 0 before admission, the in-flight step's index + 1 afterwards.
    ``reaffirmed_at`` is when a newer authenticated snapshot last showed the
    unit's structure unchanged; resource-level changes observed up to then no
    longer make the plan stale (their evidence stays invalidated). Adapter,
    unclassified, and registry changes are never cleared this way.
    """
    if not isinstance(index, ImpactIndex):
        raise ContractError("Impact index required")
    if type(events) is not tuple or any(not isinstance(e, ChangeEvent) for e in events):
        raise ContractError("Immutable change events required")
    if type(watermarks) is not tuple or any(not isinstance(w, FeedWatermark) for w in watermarks):
        raise ContractError("Immutable feed watermarks required")
    if type(issued) is not int or not 0 <= issued <= len(index.operations):
        raise ContractError("Issued step count out of range")
    if not isinstance(max_feed_lag, timedelta) or not (
        timedelta(0) < max_feed_lag <= MAX_FEED_LAG
    ):
        raise ContractError("Feed lag must be positive and at most the platform bound")
    now = utc(now)
    if len({e.event_id for e in events}) != len(events):
        raise ContractError("Duplicate change event IDs")
    structure_as_of = index.snapshot_observed_at
    if reaffirmed_at is not None:
        if utc(reaffirmed_at) > now:
            raise ContractError("Reaffirmation is in the future")
        structure_as_of = max(structure_as_of, utc(reaffirmed_at))

    side_of = dict(index.side_of_environment)
    member = {uid: node for _, uid, node in index.members}
    reasons, stale, unknown, plan_stale, plan_unknown = set(), {}, set(), False, False

    # Feed completeness first: a gap makes conclusions UNKNOWN, not "unchanged".
    latest_mark = {}
    for mark in watermarks:
        if mark.observed_through > now:
            raise ContractError("Feed watermark is in the future")
        prior = latest_mark.get(mark.environment_id)
        if prior is None or mark.observed_through > prior:
            latest_mark[mark.environment_id] = mark.observed_through
    for feed in sorted(index.required_feeds):
        mark = latest_mark.get(feed)
        if mark is None or now - mark > max_feed_lag:
            plan_unknown = True
            reasons.add(f"CHANGE_FEED_INCOMPLETE:{feed}")
            side = side_of.get(feed)  # None for the registry feed: plan inputs only
            if side is not None:
                unknown |= {o[0] for o in index.obligations if side in o[1]}

    def invalidate(predicate, at, reason):
        reasons.add(reason)
        for oid, sides, touched, kind, verifier in index.obligations:
            if predicate(sides, touched, kind, verifier):
                stale[oid] = max(stale.get(oid, at), at)

    own_steps = {step: (side, written)
              for step, side, written, _ in index.operations[:issued]}
    registry = {}
    for event in sorted(events, key=lambda e: (e.observed_at, e.event_id)):
        if event.tenant_id != index.tenant_id:
            raise ContractError("Change event from another tenant")
        if event.observed_at > now:
            raise ContractError("Change event is in the future")
        side = side_of.get(event.environment_id) if event.environment_id else None
        if event.kind in _REGISTRY:
            if event.kind == ChangeKind.CONTRACT:
                if event.application_id == index.application_id:
                    registry.setdefault(f"contract:{event.application_id}", []).append(event)
            elif event.subject is not None:
                registry.setdefault(f"{event.kind}:{event.subject}", []).append(event)
            elif event.observed_at > index.snapshot_observed_at:
                plan_stale = True  # some revision changed, unknown which: widen
                reasons.add(f"PLAN_INPUT_UNRESOLVED:{event.kind}")
            continue
        node = member.get(event.subject) if side == "source" and event.subject else None
        if _expected(event, index.plan_digest, own_steps, node, side):
            continue
        at, kind = event.observed_at, event.kind
        if kind in (ChangeKind.RESOURCE, ChangeKind.SCHEMA):
            if node is not None:
                invalidate(lambda s, t, k, v: ("source", node) in t, at,
                           f"UNIT_RESOURCE_CHANGED:{node}")
                if at > structure_as_of:
                    plan_stale = True
            elif side is not None and (event.subject is None or side != "source"):
                # Unknown resource in a plan environment: widen to that side.
                invalidate(lambda s, t, k, v: side in s, at, f"RESOURCE_CHANGED:{side}")
                if side == "source" and at > structure_as_of:
                    plan_stale = True
        elif kind == ChangeKind.POLICY and side is not None:
            # Any evidence observing this environment must be re-verified; for an
            # environment-wide permission change that includes the write-denial
            # probe, which is what catches a newly granted writer path.
            invalidate(lambda s, t, k, v: side in s, at, f"POLICY_CHANGED:{side}")
            if node is not None:
                # A unit member's own policy (e.g. its writers) is part of the plan.
                invalidate(lambda s, t, k, v: ("source", node) in t, at,
                           f"UNIT_POLICY_CHANGED:{node}")
                if at > structure_as_of:
                    plan_stale = True
        elif kind == ChangeKind.IDENTITY and side is not None:
            invalidate(lambda s, t, k, v: side in s or k.startswith("identity_"), at,
                       f"IDENTITY_CHANGED:{side}")
        elif kind == ChangeKind.CODE and event.application_id == index.application_id:
            invalidate(lambda s, t, k, v: True, at, "SERVICE_CODE_CHANGED")
        elif kind == ChangeKind.VERIFIER and event.subject is not None:
            subject = event.subject
            invalidate(lambda s, t, k, v: v == subject, at, f"VERIFIER_CHANGED:{subject}")
        elif kind == ChangeKind.VERIFIER:
            invalidate(lambda s, t, k, v: True, at, "VERIFIER_CHANGED")
        elif kind == ChangeKind.ADAPTER and (event.subject is None
                                             or event.subject in index.adapters):
            invalidate(lambda s, t, k, v: True, at, f"ADAPTER_CHANGED:{event.subject}")
            if at > index.snapshot_observed_at:
                plan_stale = True
        elif kind == ChangeKind.UNCLASSIFIED and (side is not None
                                                  or event.environment_id is None):
            invalidate(lambda s, t, k, v: side is None or side in s, at,
                       f"UNCLASSIFIED_CHANGE:{side or 'any'}")
            if at > index.snapshot_observed_at:
                plan_stale = True

    # Registry inputs: the latest recorded revision must be the one compiled in.
    for key, compiled in index.inputs:
        recorded = registry.get(key)
        if not recorded:
            continue
        last = recorded[-1].observed_at
        latest = {e.digest for e in recorded if e.observed_at == last}
        if latest != {compiled}:  # superseded, unknown revision, or ambiguous
            plan_stale = True
            reasons.add("PLAN_INPUT_SUPERSEDED:" + key.split(":")[0])
    return ImpactAssessment(
        index.plan_digest, now, plan_stale, plan_unknown,
        tuple(sorted(stale.items())), frozenset(unknown), tuple(sorted(reasons)),
        structure_as_of,
    )


def _expected(event, plan_digest, issued, node, side):
    """A change this plan's own issued command produced inside its write scope."""
    if event.cause is None or event.cause[0] != plan_digest:
        return False
    step = issued.get(event.cause[1])
    if step is None or side is None:
        return False  # future step, unknown step, or outside the plan's environments
    if event.kind not in (ChangeKind.RESOURCE, ChangeKind.SCHEMA, ChangeKind.POLICY):
        return False
    step_side, written = step
    if node is not None:
        return ("source", node) in written
    # Target and edge resources have no UID in the plan: the step's own side.
    return side != "source" and side == step_side
