"""Identity & Access extension evaluation (MIG-EXT-IAM-001, Part II).

Validates the ``identity`` contract extension against a fixed v1 schema and
derives the identity strategy and the Identity Gate obligations. It never
assumes that passwords, MFA secrets, refresh tokens, or sessions move with a
profile export; a contract that relies on such an assumption is refused. The
platform's own operator login is out of scope here.
"""

from dataclasses import dataclass
import re

from sova.domain.contracts import ContractError
from sova.domain.service_contracts import ServiceContract

MAX_DUAL_ISSUER_HOURS = 168  # an explicit, short transition window (§8.1)
_DURATION = re.compile(r"^PT([1-9][0-9]{0,3})H$")

_SCHEMA = {
    "principal_namespace": str,
    "stable_subject_mapping_required": bool,
    "allowed_external_idps": list,
    "remove_dependencies": list,
    "allow_dual_issuer_during_transition": bool,
    "dual_issuer_max_duration": (str, type(None)),
    "password_transition_policy": str,
    "mfa": dict,
    "sessions": dict,
    "authorization": dict,
    "evidence": dict,
}
_MFA = {"required": bool, "allowed_methods": list, "reenrollment_allowed": bool,
        "user_communication_approved": bool}
_SESSIONS = {"forced_reauthentication_allowed": bool,
             "refresh_token_portability_assumed": bool}
_AUTHZ = {"deny_cross_tenant_access": bool,
          "source_target_permission_equivalence_required": bool}
_EVIDENCE = {"fresh_login_required": bool, "fresh_token_refresh_required": bool,
             "denied_access_test_required": bool,
             "source_idp_blocked_cold_start_required": bool}
_CREDENTIALS = {
    "reset": ("RESET_AND_REENROLL",),
    "verified-bridge": ("JUST_IN_TIME_ACCOUNT_BRIDGE",),
    "reset-or-verified-bridge": ("JUST_IN_TIME_ACCOUNT_BRIDGE", "RESET_AND_REENROLL"),
}


def _exact(value, schema, name):
    if type(value) is not dict or set(value) != set(schema):
        raise ContractError(f"Identity {name} fields do not match the v1 schema")
    for key, kind in schema.items():
        if not isinstance(value[key], kind) or (kind is int and type(value[key]) is bool):
            raise ContractError(f"Identity {name}.{key} has the wrong type")
        if isinstance(value[key], list) and any(
            type(v) is not str or not v or v != v.strip() for v in value[key]
        ):
            raise ContractError(f"Identity {name}.{key} must list names")


@dataclass(frozen=True, slots=True)
class IdentityEvaluation:
    strategy: str
    credential_strategies: tuple[str, ...]
    dual_issuer_hours: int | None
    retained_dependencies: tuple[str, ...]
    blockers: tuple[tuple[str, str], ...]  # (code, status impact)
    obligations: tuple[dict, ...]
    deferred: tuple[dict, ...]

    @property
    def document(self):
        return {
            "strategy": self.strategy,
            "credential_strategies": list(self.credential_strategies),
            "dual_issuer_hours": self.dual_issuer_hours,
            "retained_dependencies": list(self.retained_dependencies),
            "independence_obligations": [dict(o) for o in self.deferred],
        }


def evaluate_identity(contract):
    """None when the contract has no identity extension; otherwise a full evaluation."""
    if not isinstance(contract, ServiceContract):
        raise ContractError("Typed service contract required")
    spec = contract.document["spec"]
    if "identity" not in spec:
        return None
    identity = spec["identity"]
    _exact(identity, _SCHEMA, "identity")
    _exact(identity["mfa"], _MFA, "mfa")
    _exact(identity["sessions"], _SESSIONS, "sessions")
    _exact(identity["authorization"], _AUTHZ, "authorization")
    _exact(identity["evidence"], _EVIDENCE, "evidence")
    removing = tuple(sorted(identity["remove_dependencies"]))
    blockers = []

    def block(code, impact="REQUIRES_REMEDIATION"):
        blockers.append((code, impact))

    # Fail-closed assumptions (§1, §8.1, §14): none of these may be assumed.
    if identity["sessions"]["refresh_token_portability_assumed"]:
        block("REFRESH_TOKEN_PORTABILITY_ASSUMED")
    if not identity["authorization"]["deny_cross_tenant_access"]:
        block("CROSS_TENANT_DENIAL_REQUIRED")
    hours = None
    if identity["allow_dual_issuer_during_transition"]:
        match = _DURATION.fullmatch(identity["dual_issuer_max_duration"] or "")
        if not match or int(match.group(1)) > MAX_DUAL_ISSUER_HOURS:
            block("DUAL_ISSUER_WINDOW_UNBOUNDED")
        else:
            hours = int(match.group(1))
    elif identity["dual_issuer_max_duration"] is not None:
        block("DUAL_ISSUER_DURATION_WITHOUT_DUAL_ISSUER")

    if removing:
        strategy = "MIGRATE_IDENTITY"
        if not identity["stable_subject_mapping_required"]:
            block("STABLE_SUBJECT_MAPPING_REQUIRED")
        credentials = _CREDENTIALS.get(identity["password_transition_policy"])
        if credentials is None:
            block("CREDENTIAL_STRATEGY_UNSUPPORTED", "UNSUPPORTED")
            credentials = ()
        mfa = identity["mfa"]
        if mfa["required"] and not mfa["reenrollment_allowed"]:
            # MFA secrets are not exported; re-enrollment is the only qualified path.
            block("MFA_TRANSFER_UNSUPPORTED", "UNSUPPORTED")
        if mfa["reenrollment_allowed"] and not mfa["user_communication_approved"]:
            block("USER_COMMUNICATION_NOT_APPROVED")
        if (
            not identity["sessions"]["forced_reauthentication_allowed"]
            and not identity["allow_dual_issuer_during_transition"]
        ):
            block("REAUTHENTICATION_FORBIDDEN_WITHOUT_DUAL_ISSUER")
        if not set(removing) <= set(spec["exit"]["remove"]):
            # The exit contract is what independence is later tested against.
            block("IDENTITY_REMOVAL_NOT_IN_EXIT_CONTRACT")
        if set(removing) & set(identity["allowed_external_idps"]):
            block("REMOVED_IDP_STILL_ALLOWED")
        retained = ()
    else:
        strategy, credentials = "RETAIN_SOURCE_IDP", ()
        retained = tuple(sorted(identity["allowed_external_idps"]))
    if not identity["allowed_external_idps"]:
        block("TARGET_IDP_UNDECLARED")
    # Every IdP the service still relies on after the handoff is a remaining
    # dependency; it must be one the exit contract explicitly allows (IAM-AT-01/02).
    if set(identity["allowed_external_idps"]) - set(spec["exit"]["allowed_remaining"]):
        block("IDP_NOT_ALLOWED_BY_EXIT_CONTRACT")

    obligations = [
        ("IAM-04.cross_tenant_denial", "identity_flow_test", "record_activation_intent"),
        ("IAM-07.no_secret_or_pii_leak", "identity_leak_scan", "stage_target"),
        ("IAM-09.audit_lock_recovery_paths", "identity_operations_check", "stabilize"),
        ("IAM-12.login_slo", "measured_budget", "stabilize"),
    ]
    if identity["evidence"]["fresh_login_required"]:
        obligations.append(("IAM-02.fresh_login_and_validation", "identity_flow_test",
                            "record_activation_intent"))
    if identity["evidence"]["fresh_token_refresh_required"]:
        obligations.append(("IAM-02.fresh_token_refresh", "identity_flow_test",
                            "record_activation_intent"))
    if identity["mfa"]["required"]:
        obligations.append(("IAM-03.mfa_and_recovery", "identity_flow_test",
                            "record_activation_intent"))
    if removing:
        obligations += [
            ("IAM-01.subject_mapping_unique", "identity_mapping_check", "verify_final_state"),
            ("IAM-06.account_state_parity", "identity_mapping_check", "verify_final_state"),
        ]
        if identity["authorization"]["source_target_permission_equivalence_required"]:
            obligations.append(("IAM-04.permission_equivalence", "identity_flow_test",
                                "record_activation_intent"))
    if hours is not None:
        obligations.append(("IAM-05.dual_issuer_acceptance_policy", "identity_flow_test",
                            "commit_route"))
    deferred = []
    if removing:
        if identity["evidence"]["source_idp_blocked_cold_start_required"]:
            deferred.append({"id": "IAM-08.source_idp_blocked_cold_start",
                             "kind": "independence_drill"})
        deferred.append({"id": "IAM-10.old_issuer_rejected_after_end",
                         "kind": "independence_drill"})
        if hours is not None or "JUST_IN_TIME_ACCOUNT_BRIDGE" in credentials:
            deferred.append({"id": "IAM-11.bridge_and_dual_issuer_retired",
                             "kind": "independence_drill"})
    return IdentityEvaluation(
        strategy,
        tuple(credentials),
        hours,
        retained,
        tuple(sorted(set(blockers))),
        tuple({"id": i, "kind": k, "required_at": at} for i, k, at in sorted(obligations)),
        tuple(sorted(deferred, key=lambda d: d["id"])),
    )
