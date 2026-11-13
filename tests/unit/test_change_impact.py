"""INV-08 change impact over a compiled, lowered plan (synthetic inputs)."""

from datetime import timedelta
import json

from change_support import memory_changes
from contract_world import build_world
import pytest

from sova.application.handoff_coordinator import HandoffCoordinator
from sova.domain.change_impact import (
    ChangeEvent,
    ChangeKind,
    FeedWatermark,
    ImpactIndex,
    PlanReevaluationRequired,
    assess_impact,
)
from sova.domain.contracts import ContractError
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.handoff_authority import EffectOutcome
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan

SOURCE, TARGET, DB = "synthetic-source", "onprem-target", "synthetic-uid-database"


@pytest.fixture
def compiled(now):
    world = build_world(now)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, result = compile_revision(
        world["contract"], world["snapshot"],
        requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    assert result["status"] == "COMPILED"
    doc = json.loads(revision.canonical)
    kinds = {o["kind"] for o in doc["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    index = ImpactIndex.from_plan(revision, plan)
    return dict(revision=revision, plan=plan, index=index, doc=doc, world=world,
                tenant=doc["tenant_id"], app=doc["service_id"])


def event(c, kind, *, at, env=None, subject=None, digest=None, cause=None, app=None,
          event_id=None):
    event.n = getattr(event, "n", 0) + 1
    return ChangeEvent(event_id or f"ev-{event.n}", c["tenant"], app or c["app"], kind, env,
                       subject, digest, at, cause)


def assess(c, events, *, now, issued=0, gaps=(), marks=None):
    if marks is None:
        marks = tuple(FeedWatermark(f, now) for f in c["index"].required_feeds
                      if f not in gaps)
    return assess_impact(c["index"], tuple(events), marks, issued=issued, now=now)


def by_kind(c, *kinds):
    return {o["id"] for o in c["doc"]["obligations"] if o["kind"] in kinds}


def step(c, kind):
    return next(o["operation_id"] for o in c["doc"]["operations"] if o["kind"] == kind)


def test_no_recorded_change_with_complete_feeds_allows_commands(compiled, now):
    impact = assess(compiled, (), now=now + timedelta(minutes=1))
    assert impact.commands_allowed and not impact.stale_since and not impact.unknown_obligations


def test_unit_resource_recreated_after_the_snapshot_makes_the_plan_stale(compiled, now):
    at = now + timedelta(seconds=30)
    impact = assess(compiled, [event(compiled, ChangeKind.RESOURCE, at=at, env=SOURCE,
                                     subject=DB)], now=now + timedelta(minutes=1))
    assert impact.plan_stale and not impact.commands_allowed
    invalidated = dict(impact.stale_since)
    # Evidence that observed the source database is invalidated; target placement is not.
    assert by_kind(compiled, "independent_write_denial_probe", "final_content_comparison",
                   "business_invariant") <= set(invalidated)
    assert not by_kind(compiled, "placement_evidence") & set(invalidated)
    assert invalidated["fence.source_writes_denied"] == at
    assert not impact.evidence_counts("fence.source_writes_denied", at)
    assert impact.evidence_counts("fence.source_writes_denied", at + timedelta(seconds=1))
    with pytest.raises(PlanReevaluationRequired, match="UNIT_RESOURCE_CHANGED:database"):
        impact.require_commands_allowed()


def test_a_change_before_the_snapshot_invalidates_older_evidence_only(compiled, now):
    impact = assess(compiled, [event(compiled, ChangeKind.SCHEMA, at=now, env=SOURCE,
                                     subject=DB)], now=now + timedelta(minutes=1))
    assert impact.commands_allowed  # the snapshot already reflects it
    assert not impact.evidence_counts("data.orders", now)


def test_unrelated_and_unresolved_resources(compiled, now):
    later = now + timedelta(minutes=1)
    other = assess(compiled, [event(compiled, ChangeKind.RESOURCE, at=now, env=SOURCE,
                                    subject="someone-elses-queue")], now=later)
    assert other.commands_allowed and not other.stale_since
    elsewhere = assess(compiled, [event(compiled, ChangeKind.POLICY, at=now,
                                        env="unrelated-env")], now=later)
    assert elsewhere.commands_allowed and not elsewhere.stale_since
    # Something changed in the source, unknown which: widen, never drop.
    unknown = assess(compiled, [event(compiled, ChangeKind.RESOURCE,
                                      at=now + timedelta(seconds=1), env=SOURCE)], now=later)
    assert unknown.plan_stale
    assert by_kind(compiled, "independent_write_denial_probe") <= dict(unknown.stale_since).keys()


def test_target_policy_change_invalidates_target_evidence_but_not_the_plan(compiled, now):
    impact = assess(compiled, [event(compiled, ChangeKind.POLICY, at=now, env=TARGET)],
                    now=now + timedelta(minutes=1))
    assert impact.commands_allowed
    invalidated = set(dict(impact.stale_since))
    # Target security controls are due at a platform step but observe the target.
    assert by_kind(compiled, "target_control_test", "placement_evidence") <= invalidated
    assert "fence.source_writes_denied" not in invalidated
    # An environment-wide source permission change may grant a new writer: the
    # write-denial probe must be re-run, though the plan's structure stands.
    source = assess(compiled, [event(compiled, ChangeKind.POLICY, at=now, env=SOURCE)],
                    now=now + timedelta(minutes=1))
    assert source.commands_allowed
    assert "fence.source_writes_denied" in dict(source.stale_since)


def test_code_and_verifier_changes(compiled, now):
    later = now + timedelta(minutes=1)
    every = {o["id"] for o in compiled["doc"]["obligations"]}
    code = assess(compiled, [event(compiled, ChangeKind.CODE, at=now)], now=later)
    assert set(dict(code.stale_since)) == every and code.commands_allowed
    other_app = assess(compiled, [event(compiled, ChangeKind.CODE, at=now,
                                        app="another-service")], now=later)
    assert not other_app.stale_since
    verifier = assess(compiled, [event(compiled, ChangeKind.VERIFIER, at=now,
                                       subject="v-1")], now=later)
    assert set(dict(verifier.stale_since)) == every
    unrelated = assess(compiled, [event(compiled, ChangeKind.VERIFIER, at=now,
                                        subject="v-2")], now=later)
    assert not unrelated.stale_since


def test_registry_inputs_must_still_be_the_compiled_revisions(compiled, now):
    later, inputs = now + timedelta(minutes=1), compiled["doc"]["inputs"]
    same = assess(compiled, [event(compiled, ChangeKind.CONTRACT, at=now,
                                   digest=inputs["contract_digest"])], now=later)
    assert same.commands_allowed
    newer = assess(compiled, [
        event(compiled, ChangeKind.CONTRACT, at=now, digest=inputs["contract_digest"]),
        event(compiled, ChangeKind.CONTRACT, at=now + timedelta(seconds=1), digest="b" * 64),
    ], now=later)
    assert newer.plan_stale and "PLAN_INPUT_SUPERSEDED:contract" in newer.reasons
    strategy = compiled["doc"]["strategy"]["strategy_id"]
    ambiguous = assess(compiled, [
        event(compiled, ChangeKind.MANIFEST, at=now, subject=strategy,
              digest=inputs["manifest_digest"]),
        event(compiled, ChangeKind.MANIFEST, at=now, subject=strategy, digest="c" * 64),
    ], now=later)
    assert ambiguous.plan_stale
    unresolved = assess(compiled, [event(compiled, ChangeKind.ENVIRONMENT_PROFILE,
                                         at=now + timedelta(seconds=1))], now=later)
    assert unresolved.plan_stale
    profile = assess(compiled, [event(compiled, ChangeKind.ENVIRONMENT_PROFILE, at=now,
                                      subject=TARGET, digest="d" * 64)], now=later)
    assert profile.plan_stale


def test_own_issued_command_changes_are_expected_but_not_beyond_its_write_set(compiled, now):
    plan, later = compiled["plan"], now + timedelta(minutes=1)
    fence = step(compiled, "fence_source_writes")
    fence_index = [o["operation_id"] for o in compiled["doc"]["operations"]].index(fence)
    own = event(compiled, ChangeKind.POLICY, at=now + timedelta(seconds=5), env=SOURCE,
                subject=DB, cause=(plan.fingerprint, fence))
    assert assess(compiled, [own], now=later, issued=fence_index + 1).commands_allowed
    # Attributed to a step not yet issued: treated as a foreign change.
    assert not assess(compiled, [own], now=later, issued=fence_index).commands_allowed
    # The fence writes only the database's write policy, not the API services.
    stray = event(compiled, ChangeKind.RESOURCE, at=now + timedelta(seconds=5), env=SOURCE,
                  subject="synthetic-uid-api-a", cause=(plan.fingerprint, fence))
    assert not assess(compiled, [stray], now=later, issued=fence_index + 1).commands_allowed
    other_plan = event(compiled, ChangeKind.RESOURCE, at=now + timedelta(seconds=5),
                       env=SOURCE, subject=DB, cause=("e" * 64, fence))
    assert not assess(compiled, [other_plan], now=later, issued=len(plan.steps)).commands_allowed


def test_feed_gaps_make_conclusions_unknown(compiled, now):
    later = now + timedelta(minutes=1)
    gap = assess(compiled, (), now=later, gaps=(TARGET,))
    assert gap.plan_unknown and not gap.commands_allowed
    assert by_kind(compiled, "target_control_test") <= gap.unknown_obligations
    assert "fence.source_writes_denied" not in gap.unknown_obligations
    assert not gap.evidence_counts("security.tested_restore", later)
    registry = assess(compiled, (), now=later, gaps=("sova-contract-registry",))
    assert registry.plan_unknown and not registry.unknown_obligations
    lagging = assess(compiled, (), now=later, marks=tuple(
        FeedWatermark(f, later - timedelta(minutes=20))
        for f in compiled["index"].required_feeds))
    assert lagging.plan_unknown


def test_inputs_are_validated(compiled, now):
    later = now + timedelta(minutes=1)
    with pytest.raises(ContractError, match="in the future"):
        assess(compiled, [event(compiled, ChangeKind.CODE, at=later + timedelta(seconds=1))],
               now=later)
    with pytest.raises(ContractError, match="Duplicate"):
        assess(compiled, [event(compiled, ChangeKind.CODE, at=now, event_id="x"),
                          event(compiled, ChangeKind.CODE, at=now, event_id="x")], now=later)
    with pytest.raises(ContractError, match="registry"):
        event(compiled, ChangeKind.RESOURCE, at=now, env=SOURCE, subject=DB, digest="a" * 64)
    with pytest.raises(ContractError, match="ahead|future"):
        assess(compiled, (), now=later, marks=(FeedWatermark(SOURCE, later + timedelta(1)),))
    with pytest.raises(ContractError, match="another tenant"):
        assess_impact(compiled["index"], (ChangeEvent(
            "t", "other-tenant", compiled["app"], ChangeKind.CODE, None, None, None, now),),
            (), issued=0, now=later)


class Store:
    def __init__(self):
        self.rows = {}

    def current(self, app, unit):
        return self.rows.get((app, unit))

    def open(self, record, *, actor_id):
        self.rows[(record.application_id, record.unit_digest)] = record
        return record

    def require_no_hold(self, application_id):
        return None

    def transition(self, before, after, *, action, actor_id):
        self.rows[(after.application_id, after.unit_digest)] = after
        return after


def test_coordinator_counts_only_evidence_newer_than_a_relevant_change(compiled, now):
    clock = {"now": now + timedelta(minutes=1)}
    observed = {"at": now + timedelta(seconds=10)}
    plan, revision = compiled["plan"], compiled["revision"]

    def evidence(o):
        return (EvidenceRecord(f"ev-{o.obligation_id}-{observed['at'].timestamp()}",
                               o.obligation_id, o.scope, plan.revisions, o.verifier_version,
                               observed["at"], observed["at"] + timedelta(minutes=10),
                               VerificationResult.PASS, "observation", "a" * 64),)

    feed, changes = memory_changes(lambda: clock["now"])
    coordinator = HandoffCoordinator(store=Store(), evidence=evidence, changes=changes,
                                     clock=lambda: clock["now"])
    coordinator.open(revision, plan, actor_id="coordinator")
    stage = compiled["doc"]["operations"][0]
    # A target placement policy changed after the placement evidence was observed.
    feed.recorded.append(event(compiled, ChangeKind.POLICY, at=now + timedelta(seconds=20),
                               env=TARGET))
    assert coordinator.next_command(revision, plan, operation_id="x", epoch=1)
    with pytest.raises(ContractError, match="evidence is not satisfied"):
        coordinator.apply_effect(revision, plan, operation_id=stage["operation_id"],
                                 outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
    observed["at"] = now + timedelta(seconds=30)  # re-verified after the change
    record = coordinator.apply_effect(revision, plan, operation_id=stage["operation_id"],
                                      outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
    assert record.next_operation == 1


def test_stale_plan_blocks_commands_and_platform_decisions(compiled, now):
    clock = {"now": now + timedelta(minutes=1)}
    plan, revision, ops = compiled["plan"], compiled["revision"], compiled["doc"]["operations"]

    def evidence(o):
        at = clock["now"] - timedelta(seconds=1)
        return (EvidenceRecord(f"ev-{o.obligation_id}-{at.timestamp()}", o.obligation_id,
                               o.scope, plan.revisions, o.verifier_version, at,
                               at + timedelta(minutes=10), VerificationResult.PASS,
                               "observation", "a" * 64),)

    feed, changes = memory_changes(lambda: clock["now"])
    store = Store()
    coordinator = HandoffCoordinator(store=store, evidence=evidence, changes=changes,
                                     clock=lambda: clock["now"])
    coordinator.open(revision, plan, actor_id="coordinator")
    intent = [o["kind"] for o in ops].index("record_activation_intent")
    for op in ops[:intent]:
        coordinator.apply_effect(revision, plan, operation_id=op["operation_id"],
                                 outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
    feed.recorded.append(event(compiled, ChangeKind.RESOURCE,
                               at=clock["now"] - timedelta(seconds=2), env=SOURCE,
                               subject="synthetic-uid-api-b"))
    clock["now"] += timedelta(seconds=5)
    with pytest.raises(PlanReevaluationRequired):
        coordinator.apply_effect(revision, plan, operation_id=ops[intent]["operation_id"],
                                 outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")
    record = next(iter(store.rows.values()))
    assert record.next_operation == intent and not record.activation_possible
    impact = coordinator.impact(revision, plan)
    assert impact.document["commands_allowed"] is False
    assert "UNIT_RESOURCE_CHANGED:api-b" in impact.document["reasons"]
    # A feed outage is unknown, never "unchanged".
    feed.recorded.clear()
    feed.down = True
    unknown = coordinator.impact(revision, plan)
    assert unknown.plan_unknown and "CHANGE_FEED_UNAVAILABLE" in unknown.reasons


def test_unplaced_events_are_widened_not_dropped(compiled, now):
    later = now + timedelta(minutes=1)
    every = {o["id"] for o in compiled["doc"]["obligations"]}
    # A member named by UID without an environment is still that member.
    named = assess(compiled, [event(compiled, ChangeKind.RESOURCE,
                                    at=now + timedelta(seconds=1), subject=DB)], now=later)
    assert named.plan_stale and "UNIT_RESOURCE_CHANGED:database" in named.reasons
    nowhere = assess(compiled, [event(compiled, ChangeKind.RESOURCE,
                                      at=now + timedelta(seconds=1))], now=later)
    assert nowhere.plan_stale and set(dict(nowhere.stale_since)) == every
    policy = assess(compiled, [event(compiled, ChangeKind.POLICY, at=now)], now=later)
    assert policy.commands_allowed and set(dict(policy.stale_since)) == every
    identity = assess(compiled, [event(compiled, ChangeKind.IDENTITY, at=now)], now=later)
    assert "IDENTITY_CHANGED:any" in identity.reasons


def test_a_revision_registered_before_the_snapshot_still_supersedes(compiled, now):
    """The compiled contract must be the latest registered one, not merely
    unchanged since the snapshot: a newer revision recorded earlier counts."""
    clock = {"now": now + timedelta(minutes=1)}
    feed, changes = memory_changes(lambda: clock["now"])
    feed.recorded.append(event(compiled, ChangeKind.CONTRACT, at=now - timedelta(hours=1),
                               digest=compiled["doc"]["inputs"]["contract_digest"]))
    feed.recorded.append(event(compiled, ChangeKind.CONTRACT, at=now - timedelta(minutes=5),
                               digest="f" * 64))
    impact = changes(compiled["revision"], compiled["plan"], issued=0)
    assert impact.plan_stale and "PLAN_INPUT_SUPERSEDED:contract" in impact.reasons
