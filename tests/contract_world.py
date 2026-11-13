"""Synthetic, locally signed registry inputs for contract/compiler tests.

Keys are generated per test; nothing here is live trust or a real environment.
"""

from dataclasses import replace
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.application.boundary_examples import example_dependency_snapshot
from sova.application.registry_authentication import RegistryAuthenticator
from sova.application.service_contracts import example_service_contract
from sova.domain.capability_catalog import (
    CapabilityManifest,
    EnvironmentProfile,
    EvidenceGrade,
    StrategyQualification,
)
from sova.domain.contracts import ContractError
from sova.domain.plan_compiler import required_operations
from sova.domain.registry_attestation import attestation_record, registry_binding
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


def profiles():
    source = EnvironmentProfile(
        "aws-source", 1, "aws", "ecs", "postgresql", "iam-role", "private", "arm64",
        (("ecs-platform", "1.4.0"), ("postgresql", "18.4")),
    )
    target = EnvironmentProfile(
        "onprem-target", 1, "on_prem", "linux-vm", "postgresql", "customer-owned",
        "private", "arm64", (("os", "linux-test"), ("postgresql", "18.4")),
    )
    return source, target


def build_world(now, *, contract=None, strategy="offline-copy", operations=None):
    contract = contract or example_service_contract()
    source, target = profiles()
    operations = operations or required_operations(strategy) or ("copy_content",)
    manifest = CapabilityManifest(
        "pg-" + strategy, "1.0.0", "a" * 64, strategy, "1.0.0",
        tuple(operations), (MigrationProfile.SINGLE_AUTHORITY,),
    )
    qualification = StrategyQualification(
        "test-record", contract.tenant_id, contract.service_id, contract.fingerprint,
        manifest.fingerprint, source.fingerprint, target.fingerprint,
        MigrationProfile.SINGLE_AUTHORITY, tuple(operations), 2000,
        now - timedelta(minutes=1), now + timedelta(minutes=5), "test-verifier",
        "b" * 64, "c" * 64, "d" * 64, EvidenceGrade.LIVE,
    )
    snapshot = replace(
        example_dependency_snapshot(now, contract),
        writer_inventory_complete=True,
        control_inventory_complete=True,
        unresolved_paths=(),
    )
    signer = Ed25519EvidenceSigner(
        Ed25519PrivateKey.generate(), issuer_id="registry-verifier", key_id="k1"
    )
    scopes = tuple(
        registry_binding(s).scope for s in (contract, snapshot, qualification)
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

    auth = RegistryAuthenticator(
        find_envelope=store.find_envelope,
        resolve_trust=lambda issuer, key: trusts[(issuer, key)],
        clock=lambda: clock[0],
        verifier_version=VERSION,
    )
    return dict(
        now=now, contract=contract, source=source, target=target, manifest=manifest,
        qualification=qualification, snapshot=snapshot, signer=signer, trust=trust,
        trusts=trusts, store=store, sign=sign, auth=auth, clock=clock,
    )
