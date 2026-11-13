"""Independence after a completed handoff (§20, Part II §9.2), synthetic inputs."""

import copy
from datetime import timedelta
import json

from change_support import memory_changes
from contract_world import build_world
import pytest

from sova.application.exit_readiness import ExitReadiness
from sova.application.handoff_coordinator import HandoffCoordinator
from sova.application.service_contracts import example_service_contract
from sova.domain.change_impact import ChangeEvent, ChangeKind
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.domain.exit_readiness import exit_drills
from sova.domain.handoff_authority import EffectOutcome, HandoffState
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan
from sova.domain.service_contracts import ServiceContract

IDENTITY = {
    "principal_namespace": "organization-scoped-stable-id",
    "stable_subject_mapping_required": True,
    "allowed_external_idps": ["target-oidc"],
    "remove_dependencies": ["aws-cognito-user-pool"],
    "allow_dual_issuer_during_transition": True,
    "dual_issuer_max_duration": "PT24H",
    "password_transition_policy": "reset-or-verified-bridge",
    "mfa": {"required": True, "allowed_methods": ["totp"],
            "reenrollment_allowed": True, "user_communication_approved": True},
    "sessions": {"forced_reauthentication_allowed": True,
                 "refresh_token_portability_assumed": False},
    "authorization": {"deny_cross_tenant_access": True,
                      "source_target_permission_equivalence_required": True},
    "evidence": {"fresh_login_required": True, "fresh_token_refresh_required": True,
                 "denied_access_test_required": True,
                 "source_idp_blocked_cold_start_required": True},
}
EXIT = {"remove": ["aws-cognito-user-pool", "source-db"],
        "allowed_remaining": ["customer-owned-idp", "target-oidc"]}


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


def handoff(now, identity=IDENTITY, exit=EXIT):
    data = example_service_contract().document
    if identity is not None:
        data["spec"]["identity"] = copy.deepcopy(identity)
    data["spec"]["exit"] = copy.deepcopy(exit)
    world = build_world(now, contract=ServiceContract.from_document(data))
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, result = compile_revision(
        world["contract"], world["snapshot"], requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    assert result["status"] == "COMPILED", result["blockers"]
    kinds = {o["kind"] for o in json.loads(revision.canonical)["obligations"]}
    plan = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    clock = {"now": now + timedelta(minutes=1)}
    drills = {}  # drill id -> (result, observed offset seconds)

    def evidence(o):
        if o.obligation_id in drills:
            if drills[o.obligation_id] is None:
                return ()
            result, offset = drills[o.obligation_id]
        else:
            result, offset = VerificationResult.PASS, -1
        at = clock["now"] + timedelta(seconds=offset)
        extra = ("waiver", "security-approver") if result == (
            VerificationResult.NOT_APPLICABLE) else (None, None)
        return (EvidenceRecord(f"ev-{o.obligation_id}-{at.timestamp()}", o.obligation_id,
                               o.scope, plan.revisions, o.verifier_version, at,
                               at + timedelta(minutes=10), result, "observation", "a" * 64,
                               *extra),)

    feed, changes = memory_changes(lambda: clock["now"])
    store = Store()
    coordinator = HandoffCoordinator(store=store, evidence=evidence, changes=changes,
                                     clock=lambda: clock["now"])
    readiness = ExitReadiness(store=store, evidence=evidence, changes=changes,
                              clock=lambda: clock["now"], verifier_version="v-1")
    coordinator.open(revision, plan, actor_id="coordinator")
    return dict(revision=revision, plan=plan, coordinator=coordinator, readiness=readiness,
                clock=clock, drills=drills, feed=feed, tenant=world["contract"].tenant_id,
                app=plan.application_id)


def complete(h):
    for op in json.loads(h["revision"].canonical)["operations"]:
        record = h["coordinator"].apply_effect(
            h["revision"], h["plan"], operation_id=op["operation_id"],
            outcome=EffectOutcome.EFFECT_CONFIRMED, actor_id="coordinator")
    assert record.state == HandoffState.HANDOFF_COMPLETE


def assess(h):
    return h["readiness"].assess(h["revision"], h["plan"])


def no_drill_evidence(h):
    for drill in exit_drills(h["revision"]):
        h["drills"][drill.drill_id] = (VerificationResult.UNKNOWN, -1)


def test_drills_follow_the_exit_contract_and_identity(now):
    h = handoff(now)
    ids = {d.drill_id for d in exit_drills(h["revision"])}
    assert {"exit.removed.source-db.blocked_cold_start",
            "exit.removed.aws-cognito-user-pool.blocked_cold_start",
            "exit.platform.customer_operates_after_revocation",
            "IAM-08.source_idp_blocked_cold_start", "IAM-10.old_issuer_rejected_after_end",
            "IAM-11.bridge_and_dual_issuer_retired"} <= ids
    assert not any("customer-owned-idp" in i for i in ids)  # allowed to remain


def test_independence_is_not_assessed_before_the_handoff_completes(now):
    value = assess(handoff(now))
    assert {v["status"] for v in value["levels"].values()} == {"NOT_STARTED"}
    assert value["identity"]["status"] == "IDENTITY_CUTOVER_IN_PROGRESS"
    assert value["source_retirement"]["reviewable"] is False


def test_completed_handoff_without_drills_is_not_independent(now):
    h = handoff(now)
    complete(h)
    no_drill_evidence(h)
    value = assess(h)
    assert value["handoff_state"] == "HANDOFF_COMPLETE"
    assert value["levels"]["PROVIDER_EXIT_VERIFIED"]["status"] == "UNKNOWN"
    assert value["identity"]["status"] == "IDENTITY_UNKNOWN"
    assert value["residual_dependencies"] == ["customer-owned-idp", "target-oidc"]
    assert value["source_retirement"]["reviewable"] is False
    h["drills"]["IAM-08.source_idp_blocked_cold_start"] = None  # not yet run
    assert assess(h)["identity"]["status"] == "IDENTITY_VALIDATED"


def test_all_drills_passing_makes_retirement_reviewable_not_approved(now):
    h = handoff(now)
    complete(h)
    value = assess(h)
    statuses = {k: v["status"] for k, v in value["levels"].items()}
    assert set(statuses.values()) == {"PASS"}, statuses
    assert value["identity"] == {
        "status": "SOURCE_IDENTITY_INDEPENDENT", "old_issuer_rejected_after_end": "pass",
        "bridge_and_dual_issuer_retired": "pass"}
    assert value["source_retirement"] == {"approval": "SEPARATE_APPROVAL_REQUIRED",
                                          "reviewable": True}


def test_a_failed_removal_drill_fails_provider_exit(now):
    """AT-35: with the source secret/DB blocked the service must still start."""
    h = handoff(now)
    complete(h)
    h["drills"]["exit.removed.source-db.blocked_cold_start"] = (VerificationResult.FAIL, -1)
    value = assess(h)
    assert value["levels"]["DATA_IDENTITY_INDEPENDENT"]["status"] == "FAIL"
    assert value["levels"]["PROVIDER_EXIT_VERIFIED"]["status"] == "FAIL"
    assert value["levels"]["RESTART_VERIFIED"]["status"] == "PASS"
    h["drills"]["IAM-08.source_idp_blocked_cold_start"] = (VerificationResult.FAIL, -1)
    assert assess(h)["identity"]["status"] == "IDENTITY_BLOCKED"


def test_a_change_after_the_drill_invalidates_it(now):
    h = handoff(now)
    complete(h)
    for drill in exit_drills(h["revision"]):
        h["drills"][drill.drill_id] = (VerificationResult.PASS, -30)
    h["feed"].recorded.append(ChangeEvent(
        "ev-target-policy", h["tenant"], h["app"], ChangeKind.POLICY, "onprem-target", None,
        None, h["clock"]["now"] - timedelta(seconds=10)))
    value = assess(h)
    assert value["levels"]["COMPUTE_INDEPENDENT"]["status"] == "UNKNOWN"
    assert value["levels"]["COMPUTE_INDEPENDENT"]["drills"][0]["reason"] == (
        "invalidated_by_change")
    assert "POLICY_CHANGED:target" in value["change_impact"]["reasons"]


def test_waivers_and_feed_gaps_never_prove_independence(now):
    h = handoff(now)
    complete(h)
    h["drills"]["exit.compute.source_compute_blocked"] = (
        VerificationResult.NOT_APPLICABLE, -1)
    value = assess(h)
    compute = value["levels"]["COMPUTE_INDEPENDENT"]
    assert compute["status"] == "UNKNOWN"
    assert compute["drills"][0]["reason"] == "waiver_not_accepted"
    h["drills"].clear()
    h["feed"].gaps.add("synthetic-source")
    gap = assess(h)
    assert gap["levels"]["PLATFORM_EXIT_VERIFIED"]["status"] == "UNKNOWN"
    assert gap["levels"]["PLATFORM_EXIT_VERIFIED"]["drills"][0]["reason"] == (
        "change_feed_incomplete")


@pytest.mark.parametrize("identity,expected", [
    (None, {"status": "NOT_APPLICABLE"}),
    ("retain", {"status": "SOURCE_IDP_RETAINED", "retained": ["aws-cognito-user-pool"]}),
])
def test_retained_or_absent_identity_is_reported_not_hidden(now, identity, expected):
    exit = {"remove": ["source-db"], "allowed_remaining": ["customer-owned-idp"]}
    if identity == "retain":
        identity = copy.deepcopy(IDENTITY)
        identity["remove_dependencies"] = []
        identity["allowed_external_idps"] = ["aws-cognito-user-pool"]
        exit["allowed_remaining"].append("aws-cognito-user-pool")
    h = handoff(now, identity=identity, exit=exit)
    complete(h)
    value = assess(h)
    assert value["identity"] == expected
    assert value["levels"]["PROVIDER_EXIT_VERIFIED"]["status"] == "PASS"
    if identity is not None:
        assert "aws-cognito-user-pool" in value["residual_dependencies"]
    assert not any(d.drill_id.startswith("IAM-") for d in exit_drills(h["revision"]))
