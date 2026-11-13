"""All qualifications/authenticators here are local doubles, not live evidence."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from sova.application.service_contracts import example_service_contract
from sova.domain.capability_catalog import (
    CapabilityManifest,
    EnvironmentProfile,
    EvidenceGrade,
    StrategyQualification,
    assess_strategy,
)
from sova.domain.contracts import ContractError
from sova.domain.service_contracts import MigrationProfile, ServiceContract


@pytest.fixture
def inputs(now):
    contract = example_service_contract()
    source = EnvironmentProfile(
        "aws-source",
        1,
        "aws",
        "ecs",
        "postgresql",
        "iam-role",
        "private",
        "arm64",
        (("ecs-platform", "1.4.0"), ("postgresql", "18.4")),
    )
    target = EnvironmentProfile(
        "onprem-target",
        1,
        "on_prem",
        "linux-vm",
        "postgresql",
        "customer-owned",
        "private",
        "arm64",
        (("os", "linux-test"), ("postgresql", "18.4")),
    )
    manifest = CapabilityManifest(
        "pg-offline",
        "1.0.0",
        "a" * 64,
        "offline-copy",
        "1.0.0",
        ("fence_writes", "copy_content"),
        (MigrationProfile.SINGLE_AUTHORITY,),
    )
    qualification = StrategyQualification(
        "test-record",
        "org-example",
        "orders-service",
        contract.fingerprint,
        manifest.fingerprint,
        source.fingerprint,
        target.fingerprint,
        MigrationProfile.SINGLE_AUTHORITY,
        ("fence_writes", "copy_content"),
        2000,
        now - timedelta(minutes=1),
        now + timedelta(minutes=5),
        "test-verifier",
        "b" * 64,
        "c" * 64,
        "d" * 64,
        EvidenceGrade.LIVE,
    )
    return dict(
        contract=contract,
        expected_tenant="org-example",
        source=source,
        target=target,
        manifest=manifest,
        qualification=qualification,
        required_operations=("copy_content", "fence_writes"),
        rows=2000,
        authenticate=lambda record: True,
        clock=lambda: now,
    )


def assess(inputs, **overrides):
    return assess_strategy(**(inputs | overrides))


def test_registered_direction_is_only_a_non_executable_candidate(inputs):
    value = assess(inputs)
    assert value["status"] == "QUALIFIED_CANDIDATE"
    assert (
        value["contract_satisfied"]
        is value["executable"]
        is value["authorizes_execution"]
        is False
    )
    assert value["evaluated_scope"] == "C3_DIRECTIONAL_STRATEGY_ELIGIBILITY_ONLY"
    assert value["qualification_digest"] == inputs["qualification"].fingerprint


def test_reverse_direction_is_not_implied_even_for_identical_technology(inputs):
    assert (
        assess(inputs, source=inputs["target"], target=inputs["source"])["reason"]
        == "QUALIFICATION_BINDING_MISMATCH"
    )
    source = inputs["source"]
    target = replace(source, profile_id="other-cluster-with-same-technology")
    assert source.fingerprint != target.fingerprint
    assert assess(inputs, target=target)["status"] == "NEEDS_EVIDENCE"


@pytest.mark.parametrize("qualification", [None, True, False, {"supported": True}])
def test_manifest_and_user_flags_do_not_create_qualification(inputs, qualification):
    assert (
        assess(inputs, qualification=qualification)["reason"]
        == "QUALIFICATION_REQUIRED"
    )


@pytest.mark.parametrize(
    "answer", [None, False, 0, 1, "true", (True,), {"verified": True}]
)
def test_authentication_requires_exact_true(inputs, answer):
    assert (
        assess(inputs, authenticate=lambda record: answer)["reason"]
        == "QUALIFICATION_AUTHENTICATION_FAILED"
    )


def test_no_authenticator_and_auth_failure_never_leak_raw_errors(inputs):
    assert (
        assess(inputs, authenticate=None)["reason"]
        == "QUALIFICATION_AUTHENTICATION_REQUIRED"
    )

    def fail(record):
        raise RuntimeError("private-auth-token-must-not-appear")

    value = assess(inputs, authenticate=fail)
    assert value["status"] == "NEEDS_EVIDENCE"
    assert "private-auth-token" not in json.dumps(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "other-tenant"),
        ("service_id", "other-service"),
        ("contract_digest", "f" * 64),
        ("manifest_digest", "f" * 64),
        ("source_profile_digest", "f" * 64),
        ("target_profile_digest", "f" * 64),
        ("profile", MigrationProfile.FAILBACK),
        ("grade", EvidenceGrade.LOCAL),
        ("max_rows", 1999),
        ("operations", ("fence_writes",)),
    ],
)
def test_replay_scope_direction_profile_and_limits_are_enforced(inputs, field, value):
    record = replace(inputs["qualification"], **{field: value})
    assert assess(inputs, qualification=record)["status"] == "NEEDS_EVIDENCE"


def test_expiry_boundary_future_issue_and_revocation(inputs, now):
    assert (
        assess(inputs, clock=lambda: inputs["qualification"].expires_at)["reason"]
        == "QUALIFICATION_NOT_CURRENT"
    )
    assert (
        assess(inputs, clock=lambda: now - timedelta(minutes=2))["reason"]
        == "QUALIFICATION_NOT_CURRENT"
    )
    assert (
        assess(inputs, authenticate=lambda record: False)["status"] == "NEEDS_EVIDENCE"
    )
    times = iter((now, inputs["qualification"].expires_at))
    assert (
        assess(inputs, clock=lambda: next(times))["reason"]
        == "QUALIFICATION_NOT_CURRENT"
    )
    times = iter((now, now - timedelta(seconds=1)))
    assert assess(inputs, clock=lambda: next(times))["reason"] == "CLOCK_REGRESSION"


@pytest.mark.parametrize(
    "field,value",
    [
        ("adapter_digest", "e" * 64),
        ("adapter_version", "2.0.0"),
        ("strategy_version", "2.0.0"),
    ],
)
def test_adapter_change_invalidates_qualification(inputs, field, value):
    manifest = replace(inputs["manifest"], **{field: value})
    assert (
        assess(inputs, manifest=manifest)["reason"] == "QUALIFICATION_BINDING_MISMATCH"
    )


def test_missing_operation_or_profile_is_unsupported_in_this_manifest(inputs):
    assert (
        assess(inputs, required_operations=("cdc",))["reason"]
        == "CAPABILITY_OPERATION_UNSUPPORTED"
    )
    manifest = replace(inputs["manifest"], profiles=(MigrationProfile.OBSERVE,))
    assert (
        assess(inputs, manifest=manifest)["reason"] == "CAPABILITY_PROFILE_UNSUPPORTED"
    )


def test_unevaluated_identity_cannot_be_discarded_for_eligibility(inputs):
    document = inputs["contract"].document
    document["spec"]["identity"] = {"remove_dependencies": ["cognito"]}
    contract = ServiceContract.from_document(document)
    assert (
        assess(inputs, contract=contract)["reason"]
        == "CONTRACT_EXTENSION_NOT_EVALUATED"
    )


def test_profile_fact_and_code_changes_require_requalification(inputs):
    source = replace(
        inputs["source"], versions=(("ecs-platform", "1.5.0"), ("postgresql", "18.4"))
    )
    assert assess(inputs, source=source)["reason"] == "QUALIFICATION_BINDING_MISMATCH"
    target = replace(inputs["target"], architecture="amd64")
    assert assess(inputs, target=target)["reason"] == "QUALIFICATION_BINDING_MISMATCH"


@pytest.mark.parametrize("rows", [True, -1, 2000.0, "2000", 2**53])
def test_unbounded_or_inexact_limits_fail_before_authentication(inputs, rows):
    with pytest.raises(ContractError):
        assess(inputs, rows=rows)


def test_inputs_and_profile_facts_are_immutable_and_unambiguous(inputs):
    assert (
        replace(
            inputs["source"], versions=tuple(reversed(inputs["source"].versions))
        ).fingerprint
        == inputs["source"].fingerprint
    )
    with pytest.raises(ContractError):
        replace(inputs["source"], versions=(("postgresql", "18"), ("postgresql", "17")))
    with pytest.raises(ContractError):
        replace(inputs["manifest"], operations=["copy_content"])
    with pytest.raises(ContractError):
        replace(inputs["qualification"], operations=("copy_content", "copy_content"))
    with pytest.raises(ContractError):
        assess(inputs, expected_tenant="other-tenant")
