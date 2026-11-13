"""Part II identity extension: evaluation and compilation over synthetic inputs."""

import copy
import json

from contract_world import build_world
import pytest

from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError
from sova.domain.identity_contract import evaluate_identity
from sova.domain.plan_compiler import compile_revision
from sova.domain.plan_lowering import lower_plan
from sova.domain.service_contracts import ServiceContract

MIGRATE = {
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


def contract_with(identity, exit=EXIT, **extra):
    data = example_service_contract().document
    data["spec"]["identity"] = identity
    data["spec"]["exit"] = copy.deepcopy(exit)
    data["spec"].update(extra)
    return ServiceContract.from_document(data)


def changed(path, value):
    identity = copy.deepcopy(MIGRATE)
    target = identity
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return identity


def compiled(now, contract):
    world = build_world(now, contract=contract)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    return compile_revision(
        world["contract"], world["snapshot"],
        requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )


def codes(result):
    return {b["code"] for b in result["blockers"]}


def test_migrating_identity_binds_the_identity_gate_and_defers_independence(now):
    value = evaluate_identity(contract_with(MIGRATE))
    assert value.strategy == "MIGRATE_IDENTITY" and not value.blockers
    assert value.credential_strategies == ("JUST_IN_TIME_ACCOUNT_BRIDGE", "RESET_AND_REENROLL")
    assert value.dual_issuer_hours == 24
    ids = {o["id"] for o in value.obligations}
    assert {"IAM-01.subject_mapping_unique", "IAM-02.fresh_login_and_validation",
            "IAM-03.mfa_and_recovery", "IAM-04.cross_tenant_denial",
            "IAM-05.dual_issuer_acceptance_policy", "IAM-06.account_state_parity",
            "IAM-07.no_secret_or_pii_leak"} <= ids
    assert {d["id"] for d in value.deferred} == {
        "IAM-08.source_idp_blocked_cold_start", "IAM-10.old_issuer_rejected_after_end",
        "IAM-11.bridge_and_dual_issuer_retired"}

    revision, result = compiled(now, contract_with(MIGRATE))
    assert result["status"] == "COMPILED", result["blockers"]
    plan = json.loads(revision.canonical)
    at = {o["operation_id"]: o["kind"] for o in plan["operations"]}
    by_id = {o["id"]: o for o in plan["obligations"]}
    assert at[by_id["IAM-04.cross_tenant_denial"]["required_at"]] == "record_activation_intent"
    assert at[by_id["IAM-05.dual_issuer_acceptance_policy"]["required_at"]] == "commit_route"
    assert at[by_id["IAM-01.subject_mapping_unique"]["required_at"]] == "verify_final_state"
    assert plan["deferred"]["exit_contract"]["identity"]["strategy"] == "MIGRATE_IDENTITY"
    kinds = {o["kind"] for o in plan["obligations"]}
    lowered = lower_plan(revision, created_by="planner", verifiers={k: "v-1" for k in kinds})
    assert len(lowered.obligations) == len(plan["obligations"])


def test_retaining_the_source_idp_is_explicit_and_not_independent(now):
    """IAM-AT-01: the retained IdP is shown as a remaining dependency, not hidden."""
    identity = changed(("remove_dependencies",), [])
    identity["allowed_external_idps"] = ["aws-cognito-user-pool"]
    allowed = {"remove": ["source-db"],
               "allowed_remaining": ["aws-cognito-user-pool", "customer-owned-idp"]}
    value = evaluate_identity(contract_with(identity, allowed))
    assert value.strategy == "RETAIN_SOURCE_IDP" and not value.blockers
    assert value.retained_dependencies == ("aws-cognito-user-pool",)
    assert not value.deferred  # nothing to prove independent of: it stays
    assert "IAM-01.subject_mapping_unique" not in {o["id"] for o in value.obligations}
    revision, result = compiled(now, contract_with(identity, allowed))
    assert result["status"] == "COMPILED"
    exit_contract = json.loads(revision.canonical)["deferred"]["exit_contract"]
    assert exit_contract["identity"]["retained_dependencies"] == ["aws-cognito-user-pool"]


def test_full_exit_with_a_retained_idp_needs_remediation(now):
    """IAM-AT-02: the exit contract removes Cognito, but identity keeps it."""
    identity = changed(("remove_dependencies",), [])
    identity["allowed_external_idps"] = ["aws-cognito-user-pool"]
    result = compiled(now, contract_with(identity))[1]
    assert result["status"] == "REQUIRES_REMEDIATION"
    assert "IDP_NOT_ALLOWED_BY_EXIT_CONTRACT" in codes(result)


def test_identity_removal_must_agree_with_the_exit_contract(now):
    loose = {"remove": ["source-db"], "allowed_remaining": ["target-oidc"]}
    result = compiled(now, contract_with(MIGRATE, loose))[1]
    assert "IDENTITY_REMOVAL_NOT_IN_EXIT_CONTRACT" in codes(result)
    identity = changed(("allowed_external_idps",), ["target-oidc", "aws-cognito-user-pool"])
    result = compiled(now, contract_with(identity))[1]
    assert {"REMOVED_IDP_STILL_ALLOWED", "IDP_NOT_ALLOWED_BY_EXIT_CONTRACT"} <= codes(result)


@pytest.mark.parametrize(
    "path,value,code,status",
    [
        (("sessions", "refresh_token_portability_assumed"), True,
         "REFRESH_TOKEN_PORTABILITY_ASSUMED", "REQUIRES_REMEDIATION"),
        (("mfa", "reenrollment_allowed"), False,
         "MFA_TRANSFER_UNSUPPORTED", "UNSUPPORTED"),
        (("mfa", "user_communication_approved"), False,
         "USER_COMMUNICATION_NOT_APPROVED", "REQUIRES_REMEDIATION"),
        (("stable_subject_mapping_required",), False,
         "STABLE_SUBJECT_MAPPING_REQUIRED", "REQUIRES_REMEDIATION"),
        (("authorization", "deny_cross_tenant_access"), False,
         "CROSS_TENANT_DENIAL_REQUIRED", "REQUIRES_REMEDIATION"),
        (("dual_issuer_max_duration",), "P30D",
         "DUAL_ISSUER_WINDOW_UNBOUNDED", "REQUIRES_REMEDIATION"),
        (("dual_issuer_max_duration",), "PT9999H",
         "DUAL_ISSUER_WINDOW_UNBOUNDED", "REQUIRES_REMEDIATION"),
        (("password_transition_policy",), "copy-hashes",
         "CREDENTIAL_STRATEGY_UNSUPPORTED", "UNSUPPORTED"),
        (("allowed_external_idps",), [],
         "TARGET_IDP_UNDECLARED", "REQUIRES_REMEDIATION"),
    ],
)
def test_unsafe_identity_assumptions_never_compile(now, path, value, code, status):
    result = compiled(now, contract_with(changed(path, value)))[1]
    assert result["status"] == status
    assert code in codes(result)
    assert result["plan"] is None


def test_no_reauthentication_needs_a_bounded_dual_issuer(now):
    identity = changed(("sessions", "forced_reauthentication_allowed"), False)
    identity["allow_dual_issuer_during_transition"] = False
    identity["dual_issuer_max_duration"] = None
    result = compiled(now, contract_with(identity))[1]
    assert "REAUTHENTICATION_FORBIDDEN_WITHOUT_DUAL_ISSUER" in codes(result)


def test_unknown_identity_fields_are_rejected(now):
    identity = copy.deepcopy(MIGRATE)
    identity["copy_password_hashes"] = True
    with pytest.raises(ContractError, match="v1 schema"):
        evaluate_identity(contract_with(identity))
    identity = copy.deepcopy(MIGRATE)
    identity["mfa"]["required"] = "yes"
    with pytest.raises(ContractError, match="wrong type"):
        evaluate_identity(contract_with(identity))


def test_other_extensions_still_hold_the_plan(now):
    contract = contract_with(MIGRATE, security_hold_policy={"mode": "hold-on-incident"})
    result = compiled(now, contract)[1]
    assert result["status"] == "NEEDS_EVIDENCE"
    assert "CONTRACT_EXTENSION_NOT_EVALUATED" in codes(result)


def test_contract_without_identity_has_no_identity_section(now):
    assert evaluate_identity(example_service_contract()) is None
    revision, _ = compiled(now, example_service_contract())
    assert json.loads(revision.canonical)["deferred"]["exit_contract"]["identity"] is None
