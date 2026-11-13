"""Post-activation source fence watch: fenced, reactivated, or unknown (AT-32)."""

from datetime import timedelta
import json

from contract_world import build_world
import pytest

from sova.application.source_fence_watch import SourceFenceWatch
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.handoff_authority import EffectOutcome, complete_operation, start
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan


class Holds:
    def __init__(self):
        self.opened = []

    def open_incident(self, scope, reason, raised_by):
        class Stored:
            class hold:
                hold_id = "incident-1"
        self.opened.append((scope, reason))
        return Stored, len(self.opened) == 1


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
        clock=lambda: now)
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    record = start(revision, plan_digest=plan.fingerprint)
    rows = {"record": record}
    verdict = {"result": None}

    class Store:
        def current(self, app, unit):
            return rows["record"]

    def evidence(o):
        if verdict["result"] is None:
            return ()
        return (EvidenceRecord("ev-fence", o.obligation_id, o.scope, plan.revisions,
                               o.verifier_version, now, now + timedelta(minutes=5),
                               verdict["result"], "observation", "a" * 64),)

    holds = Holds()
    watch = SourceFenceWatch(store=Store(), evidence=evidence, holds=holds,
                             clock=lambda: now + timedelta(seconds=1))
    return dict(revision=revision, plan=plan, rows=rows, verdict=verdict, holds=holds,
                watch=watch)


def activate(h):
    for op in json.loads(h["revision"].canonical)["operations"]:
        h["rows"]["record"] = complete_operation(
            h["rows"]["record"], h["revision"], operation_id=op["operation_id"],
            outcome=EffectOutcome.EFFECT_CONFIRMED,
            satisfied_evidence=frozenset(op["required_evidence"]))
        if op["kind"] == "record_activation_intent":
            return


def check(h):
    return h["watch"].check(h["revision"], h["plan"], actor_id="fence-watch")


def test_the_watch_starts_only_once_activation_may_have_happened(h):
    h["verdict"]["result"] = VerificationResult.FAIL
    assert check(h)["status"] == "NOT_APPLICABLE" and not h["holds"].opened
    activate(h)
    assert check(h)["status"] == "SOURCE_WRITER_REACTIVATED"
    assert h["holds"].opened[0][1] == "SOURCE_WRITER_REACTIVATED"


def test_no_observation_is_unknown_never_fenced(h):
    activate(h)
    assert check(h) == {"status": "UNKNOWN", "reason": "missing_bound_evidence"}
    h["verdict"]["result"] = VerificationResult.PASS
    assert check(h)["status"] == "FENCED" and not h["holds"].opened
