"""Clean failback over real PostgreSQL stores (§15.4, §19.3, AT-30).

A forward handoff that activated the target and then breached its contract is
failed back by a separate, approved plan: the failback takes over the forward
claims in one transaction, continues the authority epoch, and returns business
authority to the original source resources. Synthetic inputs; local keys.
"""

from datetime import UTC, datetime, timedelta
import json

from contract_world import build_world
from handoff_support import admitted_handoff, complete_feeds
import pytest

from sova.application.handoff_admission import HandoffAdmission
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.domain.plan_compiler import FAILBACK_STRATEGY, compile_failback
from sova.domain.plan_lowering import lower_plan
from sova.domain.plans import PlanApproval, PlanPurpose
from sova.ports.plans import RepositoryConflict

pytestmark = pytest.mark.postgres


def step(h, revision, plan, op):
    for obligation_id in op["required_evidence"]:
        o = next(x for x in plan.obligations if x.obligation_id == obligation_id)
        h["prove"](obligation_id, obligation=o, revisions=plan.revisions)
    return h["coordinator"].apply_effect(revision, plan, operation_id=op["operation_id"],
                                         outcome=EffectOutcome.EFFECT_CONFIRMED,
                                         actor_id="coordinator")


@pytest.fixture
def failed_forward(postgres_database, now):
    h = admitted_handoff(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    for op in h["ops"]:
        if op["kind"] == "commit_route":
            break
        step(h, h["revision"], h["plan"], op)
    record = h["coordinator"].require_failback(
        h["revision"], h["plan"], reason="contract breach after activation",
        actor_id="coordinator")
    assert record.state == HandoffState.FAILBACK_REQUIRED
    return h


def prepare_failback(h, now, *, world=None, approval_id="approval-failback"):
    world = world or build_world(now, contract=h["world"]["contract"],
                                 strategy=FAILBACK_STRATEGY, reverse=True,
                                 environment="synthetic-target", uid_prefix="target-uid-")
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    unit = json.loads(h["revision"].canonical)["unit"]["unit_digest"]

    def recompile():
        return compile_failback(
            h["revision"], h["store"].current(h["app"], unit), world["contract"],
            world["snapshot"], requested_nodes=("api-a", "api-b", "database"),
            expected_tenant=h["tenant"], source=world["source"], target=world["target"],
            manifest=world["manifest"], qualification=world["qualification"], rows=2000,
            authenticate=world["auth"], clock=lambda: datetime.now(UTC))

    revision, result = recompile()
    assert result["status"] == "COMPILED", result["blockers"]
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    verifiers = {k: h["verifier"] for k in kinds}
    plan = lower_plan(revision, created_by="planner", verifiers=verifiers)
    h["lifecycle"].save_plan(plan)
    h["lifecycle"].save_approval(h["app"], plan.plan_id, approval_id, PlanApproval(
        plan.fingerprint, "security-approver", PlanPurpose.MIGRATION_PHASE,
        now + timedelta(minutes=3)))
    h["trust_scopes"]({o.scope for o in plan.obligations})
    complete_feeds(h["feed"], revision, plan)
    return dict(revision=revision, plan=plan, recompile=recompile, verifiers=verifiers)


def admit(h, fb, *, approval_id="approval-failback"):
    return HandoffAdmission(lifecycle=h["lifecycle"], claims=h["claims"],
                            changes=h["changes"], clock=lambda: datetime.now(UTC)).admit(
        h["app"], approval_id, recompile=fb["recompile"], verifiers=fb["verifiers"])


def test_failback_takes_over_claims_and_returns_authority_to_the_source(failed_forward,
                                                                        now):
    h = failed_forward
    forward_record = h["store"].current(
        h["app"], json.loads(h["revision"].canonical)["unit"]["unit_digest"])
    fb = prepare_failback(h, now)
    assert admit(h, fb)["status"] == "ADMITTED"
    active = {plan for _, plan, _, _ in h["claims"].active()}
    assert active == {fb["plan"].fingerprint}  # forward claims passed, not overlapping
    keys = {key for _, _, key, _ in h["claims"].active()}
    assert ("source", "synthetic-source", "synthetic-uid-database") in keys

    record = h["coordinator"].open(fb["revision"], fb["plan"], actor_id="coordinator")
    assert record.epoch == forward_record.epoch + 1
    assert record.predecessor == h["revision"].digest
    for op in json.loads(fb["revision"].canonical)["operations"]:
        record = step(h, fb["revision"], fb["plan"], op)
    assert record.state == HandoffState.HANDOFF_COMPLETE
    assert record.owner_environment == "aws-source"
    # The forward handoff stays terminal; it cannot be failed back twice.
    assert h["store"].current(h["app"], forward_record.unit_digest).state == (
        HandoffState.FAILBACK_REQUIRED)
    history = h["store"].history(h["app"], record.unit_digest)
    assert history[0][1] == "opened" and history[0][3].predecessor == h["revision"].digest


def test_failback_admission_requires_the_forward_handoff_to_need_it(postgres_database,
                                                                     now):
    h = admitted_handoff(postgres_database, now)
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    with pytest.raises(RepositoryConflict, match="does not require failback"):
        h["claims"].acquire(
            application_id=h["app"], plan_id=h["plan"].plan_id,
            plan_digest=h["plan"].fingerprint, compiled_digest=h["revision"].digest,
            approval_id="approval-1",
            approval_revision=h["lifecycle"].get_approval(h["app"], "approval-1").revision,
            claims=((("route", "x", "y"), "write"),), supersedes=h["plan"].fingerprint)


def test_a_forward_handoff_is_failed_back_at_most_once(failed_forward, now):
    h = failed_forward
    first = prepare_failback(h, now)
    admit(h, first)
    h["coordinator"].open(first["revision"], first["plan"], actor_id="coordinator")
    for op in json.loads(first["revision"].canonical)["operations"]:
        step(h, first["revision"], first["plan"], op)
    h["claims"].release(application_id=h["app"], plan_digest=first["plan"].fingerprint,
                        actor_id="handoff-coordinator")
    # A second failback (another observation of the unit) passes admission because
    # the forward record still says FAILBACK_REQUIRED, but cannot take authority.
    world = build_world(now, contract=h["world"]["contract"], strategy=FAILBACK_STRATEGY,
                        reverse=True, environment="synthetic-target",
                        uid_prefix="target-uid-2-")
    second = prepare_failback(h, now, world=world, approval_id="approval-failback-2")
    admit(h, second, approval_id="approval-failback-2")
    with pytest.raises(RepositoryConflict, match="Another failback"):
        h["coordinator"].open(second["revision"], second["plan"], actor_id="coordinator")
