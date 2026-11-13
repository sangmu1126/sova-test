"""Registry authentication over the real signed-evidence and trust stores.

Local Ed25519 keys and a synthetic contract; no live environment is attested.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import psycopg
import pytest

from sova.adapters.ed25519_evidence import (
    Ed25519EvidenceSigner,
    Ed25519EvidenceVerifier,
)
from sova.adapters.postgres_signed_evidence import PostgresSignedEvidence
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.boundary_examples import example_dependency_snapshot
from sova.application.registry_authentication import postgres_registry_authenticator
from sova.application.service_contracts import example_service_contract
from sova.application.signed_evidence import SignedEvidenceAuthority
from sova.domain.contracts import ContractError
from sova.domain.migration_units import analyze_boundary
from sova.domain.registry_attestation import attestation_record, registry_binding
from sova.domain.service_contracts import ServiceContract
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import VerifierTrustChange

pytestmark = pytest.mark.postgres
VERSION = "registry-attestor-1"


@pytest.fixture
def registry_world(postgres_database, now):
    dsn, role = postgres_database
    tenant = "tenant-" + uuid4().hex
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = tenant
    contract = ServiceContract.from_document(data)
    snapshot = replace(
        example_dependency_snapshot(now, contract),
        writer_inventory_complete=True,
        control_inventory_complete=True,
        unresolved_paths=(),
    )

    def connect():
        return psycopg.connect(dsn, user=role)

    signer = Ed25519EvidenceSigner(
        Ed25519PrivateKey.generate(), issuer_id="registry-verifier", key_id="k1"
    )
    trust = VerifierTrust(
        signer.issuer_id,
        signer.key_id,
        signer.public_key_hex,
        (registry_binding(contract).scope, registry_binding(snapshot).scope),
        (VERSION,),
        now - timedelta(minutes=1),
        now + timedelta(hours=1),
    )
    change = VerifierTrustChange(
        tenant,
        contract.service_id,
        1,
        trust,
        "registry-requester",
        "registry-approver",
        "Register the contract registry verifier",
        now,
        now + timedelta(minutes=1),
    )
    registry = PostgresVerifierRegistry(
        connect, tenant_id=tenant, application_id=contract.service_id
    )
    registry.publish(change)
    authority = SignedEvidenceAuthority(
        PostgresSignedEvidence(connect, tenant_id=tenant),
        Ed25519EvidenceVerifier(registry.resolve),
        clock=lambda: datetime.now(UTC),
    )

    def ingest(subject, **kwargs):
        record = attestation_record(
            subject,
            verifier_version=VERSION,
            observed_at=now - timedelta(seconds=5),
            valid_until=now + timedelta(minutes=30),
            **kwargs,
        )
        return authority.ingest(signer.sign(record, issued_at=now))

    auth = postgres_registry_authenticator(
        connect,
        tenant_id=tenant,
        application_id=contract.service_id,
        verifier_version=VERSION,
        clock=lambda: datetime.now(UTC),
    )
    return dict(
        tenant=tenant, contract=contract, snapshot=snapshot, change=change,
        registry=registry, ingest=ingest, auth=auth, now=now, connect=connect,
    )


def test_ingested_attestations_authenticate_documents_after_restart(registry_world):
    w = registry_world
    with pytest.raises(ContractError):
        w["auth"](w["contract"])
    w["ingest"](w["contract"])
    w["ingest"](w["snapshot"])
    assert w["auth"](w["contract"]) is True
    value = analyze_boundary(
        w["contract"],
        w["snapshot"],
        requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=w["tenant"],
        authenticate=w["auth"],
        clock=lambda: datetime.now(UTC),
    )
    assert value["status"] == "PARTITIONABLE"
    assert value["executable"] is False


def test_trust_revocation_and_reattestation_fail_closed_in_the_store(registry_world):
    w = registry_world
    w["ingest"](w["contract"])
    w["ingest"](w["contract"], evidence_id="second-attestation")
    with pytest.raises(ContractError, match="unavailable"):
        w["auth"](w["contract"])
    w["ingest"](w["snapshot"])
    assert w["auth"](w["snapshot"]) is True
    revoked = replace(
        w["change"], revision=2, trust=replace(w["change"].trust, revoked=True)
    )
    w["registry"].publish(revoked)
    with pytest.raises(ContractError, match="revoked"):
        w["auth"](w["snapshot"])


def test_another_tenant_cannot_read_or_reuse_the_attestation(registry_world):
    w = registry_world
    w["ingest"](w["contract"])
    other = "tenant-" + uuid4().hex
    scope = registry_binding(w["contract"]).scope
    # Same application, kind, and digest under another tenant: RLS hides the row.
    with pytest.raises(ContractError, match="unambiguous"):
        PostgresSignedEvidence(w["connect"], tenant_id=other).find_envelope(
            replace(scope, tenant_id=other),
            artifact_digest=w["contract"].fingerprint,
            verifier_version=VERSION,
        )
