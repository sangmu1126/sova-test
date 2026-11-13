"""Registry documents authenticated by local Ed25519 test keys, not live trust."""

from dataclasses import replace
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.application.boundary_examples import example_dependency_snapshot
from sova.application.registry_authentication import RegistryAuthenticator
from sova.application.service_contracts import example_service_contract
from sova.domain.capability_catalog import (
    CapabilityManifest,
    EnvironmentProfile,
    EvidenceGrade,
    StrategyQualification,
    assess_strategy,
)
from sova.domain.contracts import ContractError
from sova.domain.evidence import VerificationResult
from sova.domain.migration_units import analyze_boundary
from sova.domain.registry_attestation import (
    REGISTRY_ENVIRONMENT,
    RegistryKind,
    attestation_record,
    registry_binding,
)
from sova.domain.service_contracts import MigrationProfile
from sova.domain.signed_evidence import VerifierTrust

VERSION = "registry-attestor-1"


class Store:
    """Exact-digest lookup double with the production uniqueness rule."""

    def __init__(self):
        self.envelopes = []

    def add(self, envelope):
        self.envelopes.append(envelope)

    def find_envelope(self, scope, *, artifact_digest, verifier_version):
        found = [
            e
            for e in self.envelopes
            if (
                e.evidence.scope,
                e.evidence.artifact_digest,
                e.evidence.verifier_version,
            )
            == (scope, artifact_digest, verifier_version)
        ]
        if len(found) != 1:
            raise ContractError("Exact unambiguous signed evidence identity required")
        return found[0]


@pytest.fixture
def world(now):
    contract = example_service_contract()
    source = EnvironmentProfile(
        "aws-source", 1, "aws", "ecs", "postgresql", "iam-role", "private", "arm64",
        (("ecs-platform", "1.4.0"), ("postgresql", "18.4")),
    )
    target = EnvironmentProfile(
        "onprem-target", 1, "on_prem", "linux-vm", "postgresql", "customer-owned",
        "private", "arm64", (("os", "linux-test"), ("postgresql", "18.4")),
    )
    manifest = CapabilityManifest(
        "pg-offline", "1.0.0", "a" * 64, "offline-copy", "1.0.0",
        ("fence_writes", "copy_content"), (MigrationProfile.SINGLE_AUTHORITY,),
    )
    qualification = StrategyQualification(
        "test-record", "org-example", "orders-service", contract.fingerprint,
        manifest.fingerprint, source.fingerprint, target.fingerprint,
        MigrationProfile.SINGLE_AUTHORITY, ("fence_writes", "copy_content"), 2000,
        now - timedelta(minutes=1), now + timedelta(minutes=5), "test-verifier",
        "b" * 64, "c" * 64, "d" * 64, EvidenceGrade.LIVE,
    )
    snapshot = replace(
        example_dependency_snapshot(now),
        writer_inventory_complete=True,
        control_inventory_complete=True,
        unresolved_paths=(),
    )
    signer = Ed25519EvidenceSigner(
        Ed25519PrivateKey.generate(), issuer_id="registry-verifier", key_id="k1"
    )
    scopes = tuple(
        registry_binding(subject).scope for subject in (contract, snapshot, qualification)
    )
    trust = VerifierTrust(
        signer.issuer_id, signer.key_id, signer.public_key_hex, scopes, (VERSION,),
        now - timedelta(minutes=5), now + timedelta(hours=2),
    )
    trusts = {(trust.issuer_id, trust.key_id): trust}
    store = Store()
    clock = [now]

    def sign(subject, *, until=None, **record_changes):
        record = attestation_record(
            subject,
            verifier_version=VERSION,
            observed_at=now - timedelta(seconds=30),
            valid_until=until or now + timedelta(minutes=4),
        )
        if record_changes:
            record = replace(record, **record_changes)
        envelope = signer.sign(record, issued_at=now - timedelta(seconds=10))
        store.add(envelope)
        return envelope

    authenticator = RegistryAuthenticator(
        find_envelope=store.find_envelope,
        resolve_trust=lambda issuer, key: trusts[(issuer, key)],
        clock=lambda: clock[0],
        verifier_version=VERSION,
    )
    return dict(
        now=now, contract=contract, source=source, target=target, manifest=manifest,
        qualification=qualification, snapshot=snapshot, signer=signer, trust=trust,
        trusts=trusts, store=store, sign=sign, auth=authenticator, clock=clock,
    )


def outcome(auth, subject):
    try:
        return auth(subject)
    except ContractError as exc:
        return str(exc)


def assess(world, **changes):
    inputs = dict(
        expected_tenant="org-example",
        source=world["source"],
        target=world["target"],
        manifest=world["manifest"],
        qualification=world["qualification"],
        required_operations=("copy_content", "fence_writes"),
        rows=2000,
        authenticate=world["auth"],
        clock=lambda: world["now"],
    )
    return assess_strategy(world["contract"], **(inputs | changes))


def test_signed_qualification_reaches_candidate_but_never_execution(world):
    assert assess(world)["reason"] == "QUALIFICATION_AUTHENTICATION_FAILED"
    world["sign"](world["qualification"])
    value = assess(world)
    assert value["status"] == "QUALIFIED_CANDIDATE"
    assert value["executable"] is value["authorizes_execution"] is False
    assert value["contract_satisfied"] is False


def test_signed_snapshot_lets_a_closed_boundary_become_partitionable(world):
    inputs = dict(
        requested_nodes=("api-a", "api-b", "database"),
        expected_tenant="org-example",
        authenticate=world["auth"],
        clock=lambda: world["now"],
    )
    unsigned = analyze_boundary(world["contract"], world["snapshot"], **inputs)
    assert "SNAPSHOT_AUTHENTICATION_REQUIRED" in unsigned["blockers"]
    world["sign"](world["snapshot"])
    value = analyze_boundary(world["contract"], world["snapshot"], **inputs)
    assert value["status"] == "PARTITIONABLE"
    assert value["executable"] is False


def test_contract_attestation_binds_the_exact_revision_bytes(world):
    world["sign"](world["contract"])
    assert world["auth"](world["contract"]) is True
    data = world["contract"].document
    data["spec"]["owner"] = "another-team"
    from sova.domain.service_contracts import ServiceContract

    changed = ServiceContract.from_document(data)
    assert outcome(world["auth"], changed) == "Registry attestation unavailable"


def test_signature_for_one_document_cannot_authenticate_a_changed_one(world):
    world["sign"](world["qualification"])
    for changed in (
        replace(world["qualification"], target_profile_digest="e" * 64),
        replace(world["qualification"], max_rows=1_000_000),
        replace(world["qualification"], grade=EvidenceGrade.LOCAL),
    ):
        assert outcome(world["auth"], changed) == "Registry attestation unavailable"
    uids = dict(world["snapshot"].resource_uids)
    world["sign"](world["snapshot"])
    uids["database"] = "recreated-with-the-same-name"
    recreated = replace(world["snapshot"], resource_uids=tuple(uids.items()))
    assert outcome(world["auth"], recreated) == "Registry attestation unavailable"


def test_kind_scope_and_meaning_are_part_of_the_binding(world):
    q = world["qualification"]
    binding = registry_binding(q)
    assert binding.scope.environment_id == REGISTRY_ENVIRONMENT
    assert binding.scope.resource_id == str(RegistryKind.STRATEGY_QUALIFICATION)
    world["sign"](q, obligation_id="sova.registry.dependency-snapshot.v1")
    assert outcome(world["auth"], q) == "Registry attestation binding mismatch"
    world["store"].envelopes.clear()
    world["sign"](q, revisions=registry_binding(world["contract"]).revisions)
    assert outcome(world["auth"], q) == "Registry attestation binding mismatch"
    world["store"].envelopes.clear()
    world["sign"](q, artifact_id="another-record")
    assert outcome(world["auth"], q) == "Registry attestation binding mismatch"


@pytest.mark.parametrize(
    "result", [VerificationResult.FAIL, VerificationResult.UNKNOWN]
)
def test_only_a_pass_attestation_authenticates(world, result):
    world["sign"](world["qualification"], result=result)
    assert outcome(world["auth"], world["qualification"]) != True  # noqa: E712


def test_attestation_cannot_outlive_the_document(world):
    q = world["qualification"]
    with pytest.raises(ContractError, match="outlive"):
        attestation_record(
            q,
            verifier_version=VERSION,
            observed_at=world["now"],
            valid_until=q.expires_at + timedelta(seconds=1),
        )
    world["sign"](q, valid_until=q.expires_at + timedelta(seconds=1))
    assert outcome(world["auth"], q) == "Registry attestation binding mismatch"


def test_untrusted_tampered_revoked_or_expired_keys_fail(world):
    q = world["qualification"]
    envelope = world["sign"](q)
    world["store"].envelopes[0] = replace(envelope, signature_hex="0" * 128)
    assert outcome(world["auth"], q) == "Evidence signature verification failed"
    world["store"].envelopes[0] = envelope
    key = (world["trust"].issuer_id, world["trust"].key_id)
    world["trusts"][key] = replace(world["trust"], revoked=True)
    assert "revoked" in outcome(world["auth"], q)
    world["trusts"][key] = replace(
        world["trust"], valid_until=world["now"] - timedelta(seconds=1),
        valid_from=world["now"] - timedelta(hours=1),
    )
    assert outcome(world["auth"], q) != True  # noqa: E712
    other = Ed25519EvidenceSigner(
        Ed25519PrivateKey.generate(), issuer_id="registry-verifier", key_id="k1"
    )
    world["trusts"][key] = world["trust"]
    world["store"].envelopes[0] = other.sign(envelope.evidence, issued_at=envelope.issued_at)
    assert outcome(world["auth"], q) == "Evidence signature verification failed"


def test_reattestation_or_unscoped_trust_fails_closed(world):
    q = world["qualification"]
    world["sign"](q)
    world["sign"](q, evidence_id="second-attestation")
    assert outcome(world["auth"], q) == "Registry attestation unavailable"
    world["store"].envelopes.pop()
    key = (world["trust"].issuer_id, world["trust"].key_id)
    world["trusts"][key] = replace(
        world["trust"], allowed_scopes=(registry_binding(world["contract"]).scope,)
    )
    assert outcome(world["auth"], q) == "Verifier cannot attest this scope/version"


def test_changes_during_verification_never_leave_a_stale_true(world):
    q = world["qualification"]
    envelope = world["sign"](q)
    resigned = world["signer"].sign(
        envelope.evidence, issued_at=envelope.issued_at + timedelta(seconds=1)
    )
    assert resigned != envelope
    calls = iter([envelope, resigned])
    auth = RegistryAuthenticator(
        find_envelope=lambda *a, **k: next(calls),
        resolve_trust=lambda issuer, key: world["trust"],
        clock=lambda: world["now"],
        verifier_version=VERSION,
    )
    assert outcome(auth, q) != True  # noqa: E712
    calls = []

    def revoked_after_first_check(issuer, key):
        # The first full verification sees current trust; the recheck sees revocation.
        calls.append(True)
        return world["trust"] if len(calls) <= 2 else replace(world["trust"], revoked=True)

    auth = RegistryAuthenticator(
        find_envelope=world["store"].find_envelope,
        resolve_trust=revoked_after_first_check,
        clock=lambda: world["now"],
        verifier_version=VERSION,
    )
    assert outcome(auth, q) != True  # noqa: E712
    times = iter([world["now"], world["now"] - timedelta(seconds=1)])
    auth = RegistryAuthenticator(
        find_envelope=world["store"].find_envelope,
        resolve_trust=lambda issuer, key: world["trust"],
        clock=lambda: next(times),
        verifier_version=VERSION,
    )
    assert outcome(auth, q) == "Registry clock regression"
    times = iter([world["now"], q.expires_at])
    auth = RegistryAuthenticator(
        find_envelope=world["store"].find_envelope,
        resolve_trust=lambda issuer, key: world["trust"],
        clock=lambda: next(times),
        verifier_version=VERSION,
    )
    assert outcome(auth, q) != True  # noqa: E712


def test_lookup_errors_are_sanitized(world):
    def explode(*args, **kwargs):
        raise RuntimeError("dsn=postgres://user:secret@host")

    auth = RegistryAuthenticator(
        find_envelope=explode,
        resolve_trust=lambda issuer, key: world["trust"],
        clock=lambda: world["now"],
        verifier_version=VERSION,
    )
    assert outcome(auth, world["qualification"]) == "Registry attestation unavailable"
    world["sign"](world["qualification"])
    broken_trust = RegistryAuthenticator(
        find_envelope=world["store"].find_envelope,
        resolve_trust=explode,
        clock=lambda: world["now"],
        verifier_version=VERSION,
    )
    assert (
        outcome(broken_trust, world["qualification"])
        == "Registry verifier trust unavailable"
    )
    value = assess(world, authenticate=auth)
    assert "secret" not in str(value)
    assert value["reason"] == "QUALIFICATION_AUTHENTICATION_FAILED"


def test_untyped_documents_and_missing_callbacks_are_refused(world):
    with pytest.raises(ContractError):
        registry_binding({"artifact": "json"})
    with pytest.raises(ContractError):
        RegistryAuthenticator(
            find_envelope=None,
            resolve_trust=lambda *a: None,
            clock=lambda: world["now"],
            verifier_version=VERSION,
        )
