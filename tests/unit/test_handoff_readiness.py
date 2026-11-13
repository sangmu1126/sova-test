"""Readiness view: separate execution, impact, security, independence, recovery."""

from datetime import timedelta
import json

from change_support import memory_changes
from contract_world import build_world
import pytest

from sova.application.exit_readiness import ExitReadiness
from sova.application.handoff_coordinator import HandoffCoordinator
from sova.application.handoff_readiness import HandoffReadiness
from sova.domain.change_impact import ChangeEvent, ChangeKind
from sova.domain.contracts import ContractError
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.handoff_authority import EffectOutcome
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan


class Store:
    def __init__(self):
        self.rows, self.held = {}, False

    def current(self, app, unit):
        return self.rows.get((app, unit))

    def open(self, record, *, actor_id):
        self.rows[(record.application_id, record.unit_digest)] = record
        return record

    def active_hold(self, application_id):
        return self.held

    def require_no_hold(self, application_id):
        if self.held:
            raise ContractError("Active security hold blocks handoff progress")

    def transition(self, before, after, *, action, actor_id):
        self.rows[(after.application_id, after.unit_digest)] = after
        return after


class Claims:
    def __init__(self):
        self.rows = []

    def active(self, *, application_id=None):
        return tuple(self.rows)


@pytest.fixture
def h(now):
    world = build_world(now)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, _ = compile_revision(
        world["contract"], world["snapshot"], requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    clock = {"now": now + timedelta(minutes=1)}

    def evidence(o):
        at = clock["now"] - timedelta(seconds=1)
        return (EvidenceRecord(f"ev-{o.obligation_id}", o.obligation_id, o.scope,
                               plan.revisions, o.verifier_version, at,
                               at + timedelta(minutes=10), VerificationResult.PASS,
                               "observation", "a" * 64),)

    store, claims = Store(), Claims()
    claims.rows.append((plan.application_id, plan.fingerprint, ("route", "t", "s"), "write"))
    feed, changes = memory_changes(lambda: clock["now"])
    c = HandoffCoordinator(store=store, evidence=evidence, changes=changes,
                           clock=lambda: clock["now"])
    readiness = HandoffReadiness(
        store=store, claims=claims, coordinator=c,
        exit_readiness=ExitReadiness(store=store, evidence=evidence, changes=changes,
                                     clock=lambda: clock["now"], verifier_version="v-1"))
    c.open(revision, plan, actor_id="coordinator")
    return dict(c=c, revision=revision, plan=plan, store=store, claims=claims, feed=feed,
                readiness=readiness, ops=json.loads(revision.canonical)["operations"],
                tenant=world["contract"].tenant_id, now=now)


def view(h):
    return h["readiness"].assess(h["revision"], h["plan"])


def go(h, until=None, outcome=EffectOutcome.EFFECT_CONFIRMED):
    for op in h["ops"]:
        if op["kind"] == until:
            return h["c"].apply_effect(h["revision"], h["plan"],
                                       operation_id=op["operation_id"], outcome=outcome,
                                       actor_id="c")
        h["c"].apply_effect(h["revision"], h["plan"], operation_id=op["operation_id"],
                            outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="c")


def test_fresh_handoff_is_commandable_and_abortable(h):
    v = view(h)
    assert v["commands"] == {"allowed": True, "blocked_by": []}
    assert v["execution"]["next_step"] == "stage_target"
    assert v["recovery"]["available"] == ["ABORT_BEFORE_ACTIVATION"]
    assert v["independence"]["levels"]["PROVIDER_EXIT_VERIFIED"] == "NOT_STARTED"


def test_each_blocking_condition_is_reported_separately(h):
    h["store"].held = True
    h["feed"].recorded.append(ChangeEvent(
        "ev-db", h["tenant"], "another-service", ChangeKind.RESOURCE, "synthetic-source",
        "synthetic-uid-database", None, h["now"] + timedelta(seconds=10)))
    h["feed"].gaps.add("edge")
    h["claims"].rows.clear()
    blocked = view(h)["commands"]["blocked_by"]
    assert blocked == ["SECURITY_HOLD_ACTIVE", "PLAN_STALE", "CHANGE_FEED_INCOMPLETE",
                       "CLAIMS_RELEASED"]


def test_recovery_options_follow_the_authority_model(h):
    go(h, until="fence_source_writes", outcome=EffectOutcome.EFFECT_UNKNOWN)
    v = view(h)
    assert v["execution"]["hold_reason"] == "EFFECT_UNKNOWN"
    assert v["commands"]["blocked_by"] == ["AUTHORITY_SAFE_HOLD"]
    assert v["recovery"]["available"] == ["ABORT_BEFORE_ACTIVATION", "RECONCILE"]


def test_after_activation_only_failback_remains(h):
    go(h, until="commit_route")  # route committed is next; target active
    v = view(h)
    assert v["execution"]["activation_possible"] is True
    assert v["recovery"]["available"] == ["REQUIRE_FAILBACK"]
    h["c"].require_failback(h["revision"], h["plan"], reason="breach", actor_id="c")
    assert view(h)["recovery"]["available"] == ["COMPILE_FAILBACK_PLAN"]


def test_completed_and_independent_handoff_offers_retirement_review_only(h):
    go(h)
    v = view(h)
    assert v["commands"]["blocked_by"] == ["HANDOFF_HANDOFF_COMPLETE"]
    assert set(v["independence"]["levels"].values()) <= {"PASS", "NOT_REQUIRED"}
    assert v["recovery"] == {"available": ["SOURCE_RETIREMENT_REVIEW"],
                             "after_security_hold_release": [],
                             "source_retirement": "SEPARATE_APPROVAL_REQUIRED"}


def test_security_hold_defers_recoveries_except_failback(h):
    go(h, until="fence_source_writes", outcome=EffectOutcome.EFFECT_UNKNOWN)
    h["store"].held = True
    v = view(h)["recovery"]
    assert v["available"] == []
    assert v["after_security_hold_release"] == ["ABORT_BEFORE_ACTIVATION", "RECONCILE"]
