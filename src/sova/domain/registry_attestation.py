"""Exact signed-evidence bindings for registry documents. Never a trust decision.

A registry document (service contract, dependency snapshot, strategy
qualification) is authenticated by the existing security evidence channel: a
trusted verifier signs an ``EvidenceRecord`` whose artifact digest is the
document's canonical digest. This module only derives the binding that such a
record must carry, so a signature for one kind, service, contract, profile pair,
or resource inventory can never be replayed for another. Signature checks,
issuer trust, and revocation stay in ``sova_security``.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib

from sova.domain.capability_catalog import StrategyQualification
from sova.domain.contracts import (
    ContractError,
    ResourceScope,
    canonical_json,
    identifier,
    utc,
)
from sova.domain.evidence import EvidenceRecord, RevisionSet, VerificationResult
from sova.domain.migration_units import DependencySnapshot
from sova.domain.service_contracts import ServiceContract


REGISTRY_ENVIRONMENT = "sova-contract-registry"


class RegistryKind(StrEnum):
    SERVICE_CONTRACT = "service-contract"
    DEPENDENCY_SNAPSHOT = "dependency-snapshot"
    STRATEGY_QUALIFICATION = "strategy-qualification"


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def obligation_id(kind):
    return "sova.registry." + str(RegistryKind(kind)) + ".v1"


def policy_digest(kind):
    """Pins the attestation meaning; a new meaning needs a new obligation."""
    return _digest({"policy": obligation_id(kind), "result": "authentic_document"})


@dataclass(frozen=True, slots=True)
class RegistryBinding:
    kind: RegistryKind
    scope: ResourceScope
    obligation_id: str
    artifact_id: str
    artifact_digest: str
    revisions: RevisionSet
    not_after: datetime | None

    def matches(self, record):
        """True only for the exact binding; validity/signature are checked elsewhere."""
        return (
            isinstance(record, EvidenceRecord)
            and record.scope == self.scope
            and record.obligation_id == self.obligation_id
            and record.artifact_id == self.artifact_id
            and record.artifact_digest == self.artifact_digest
            and record.revisions == self.revisions
            and record.result == VerificationResult.PASS
            and record.justification is None
            and record.approved_by is None
            and (self.not_after is None or record.valid_until <= self.not_after)
        )


def registry_binding(subject):
    """Derive the only acceptable attestation binding for a typed document."""
    if isinstance(subject, StrategyQualification):
        kind = RegistryKind.STRATEGY_QUALIFICATION
        tenant, application = subject.tenant_id, subject.service_id
        artifact_id = subject.record_id
        source = subject.contract_digest
        configuration = subject.manifest_digest
        environment = _digest(
            {
                "source_profile": subject.source_profile_digest,
                "target_profile": subject.target_profile_digest,
                "profile": str(subject.profile),
            }
        )
        not_after = subject.expires_at
    elif isinstance(subject, DependencySnapshot):
        kind = RegistryKind.DEPENDENCY_SNAPSHOT
        tenant = subject.graph.tenant_id
        application = subject.graph.application_id
        artifact_id = subject.snapshot_id
        source = subject.contract_digest
        configuration = _digest(subject.graph.document)
        environment = _digest(dict(subject.resource_uids))
        not_after = subject.graph.valid_until
    elif isinstance(subject, ServiceContract):
        kind = RegistryKind.SERVICE_CONTRACT
        tenant, application = subject.tenant_id, subject.service_id
        revision = subject.document["metadata"]["revision"]
        artifact_id = f"{subject.service_id}@{revision}"
        source = subject.fingerprint
        configuration = _digest({"revision": revision})
        environment = _digest({"tenant": tenant, "service": application})
        not_after = None
    else:
        raise ContractError("Typed registry document required")
    return RegistryBinding(
        kind,
        ResourceScope(tenant, application, REGISTRY_ENVIRONMENT, str(kind)),
        obligation_id(kind),
        identifier(artifact_id),
        subject.fingerprint,
        RevisionSet(source, configuration, policy_digest(kind), environment),
        not_after,
    )


def attestation_record(
    subject, *, verifier_version, observed_at, valid_until, evidence_id=None
):
    """Build the PASS record a trusted verifier signs after its own checks.

    Building this record is not verification. The default evidence ID is one per
    document digest, so a re-attestation needs an explicit new ID and will make
    an exact-digest lookup ambiguous (fail closed) until it is resolved.
    """
    binding = registry_binding(subject)
    observed_at, valid_until = utc(observed_at), utc(valid_until)
    if not observed_at < valid_until:
        raise ContractError("Positive attestation lifetime required")
    if binding.not_after is not None and valid_until > binding.not_after:
        raise ContractError("Attestation cannot outlive the attested document")
    return EvidenceRecord(
        evidence_id
        or f"{binding.kind}.{binding.artifact_digest}",
        binding.obligation_id,
        binding.scope,
        binding.revisions,
        identifier(verifier_version),
        observed_at,
        valid_until,
        VerificationResult.PASS,
        binding.artifact_id,
        binding.artifact_digest,
    )
