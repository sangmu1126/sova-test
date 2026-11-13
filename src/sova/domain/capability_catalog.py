"""Directional, authenticated strategy eligibility. Never grants execution.

Authentication belongs to the existing trusted evidence/registration boundary.
This module does not mint signatures, approve plans, or invoke providers. Profile
facts are frozen compiler inputs, not substitutes for live environment discovery.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import hashlib

from sova.domain.contracts import ContractError, canonical_json, digest, identifier, utc
from sova.domain.service_contracts import EXTENSIONS, MigrationProfile, ServiceContract


def _fingerprint(document):
    return hashlib.sha256(canonical_json(document, limit=65536).encode()).hexdigest()


def _names(value, *, minimum=1):
    if type(value) is not tuple or not minimum <= len(value) <= 128:
        raise ContractError("Bounded immutable capability names required")
    for name in value:
        identifier(name)
    if len(value) != len(set(value)):
        raise ContractError("Duplicate capability name")
    return tuple(sorted(value))


def _row_count(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ContractError("Bounded exact row count required")


class EvidenceGrade(StrEnum):
    LIVE = "LIVE"
    LOCAL = "LOCAL"
    UNIT = "UNIT"


@dataclass(frozen=True, slots=True)
class EnvironmentProfile:
    profile_id: str
    revision: int
    provider: str
    compute_backend: str
    data_engine: str
    identity_mechanism: str
    network_topology: str
    architecture: str
    versions: tuple[tuple[str, str], ...]

    def __post_init__(self):
        for field in (
            "profile_id",
            "provider",
            "compute_backend",
            "data_engine",
            "identity_mechanism",
            "network_topology",
            "architecture",
        ):
            identifier(getattr(self, field))
            object.__setattr__(self, field, str(getattr(self, field)))
        _row_count(self.revision)
        if self.revision == 0:
            raise ContractError("Positive profile revision required")
        if type(self.versions) is not tuple or not 1 <= len(self.versions) <= 64:
            raise ContractError("Versioned environment facts required")
        seen = set()
        for pair in self.versions:
            if type(pair) is not tuple or len(pair) != 2:
                raise ContractError("Immutable version pairs required")
            for value in pair:
                identifier(value)
            if pair[0] in seen:
                raise ContractError("Ambiguous environment version")
            seen.add(pair[0])
        object.__setattr__(self, "versions", tuple(sorted(self.versions)))

    @property
    def fingerprint(self):
        value = asdict(self)
        value["versions"] = dict(self.versions)
        return _fingerprint(value)


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    adapter_id: str
    adapter_version: str
    adapter_digest: str
    strategy_id: str
    strategy_version: str
    operations: tuple[str, ...]
    profiles: tuple[MigrationProfile, ...]

    def __post_init__(self):
        for field in (
            "adapter_id",
            "adapter_version",
            "strategy_id",
            "strategy_version",
        ):
            identifier(getattr(self, field))
        digest(self.adapter_digest)
        object.__setattr__(self, "operations", _names(self.operations))
        if (
            type(self.profiles) is not tuple
            or not self.profiles
            or any(not isinstance(p, MigrationProfile) for p in self.profiles)
        ):
            raise ContractError("Typed supported profiles required")
        object.__setattr__(self, "profiles", _names(self.profiles))

    @property
    def fingerprint(self):
        value = asdict(self)
        value["operations"] = list(self.operations)
        value["profiles"] = [str(p) for p in self.profiles]
        return _fingerprint(value)


@dataclass(frozen=True, slots=True)
class StrategyQualification:
    record_id: str
    tenant_id: str
    service_id: str
    contract_digest: str
    manifest_digest: str
    source_profile_digest: str
    target_profile_digest: str
    profile: MigrationProfile
    operations: tuple[str, ...]
    max_rows: int
    issued_at: datetime
    expires_at: datetime
    verifier_identity: str
    verifier_digest: str
    test_definition_digest: str
    raw_evidence_digest: str
    grade: EvidenceGrade

    def __post_init__(self):
        for field in ("record_id", "tenant_id", "service_id", "verifier_identity"):
            identifier(getattr(self, field))
        for field in (
            "contract_digest",
            "manifest_digest",
            "source_profile_digest",
            "target_profile_digest",
            "verifier_digest",
            "test_definition_digest",
            "raw_evidence_digest",
        ):
            digest(getattr(self, field))
        if not isinstance(self.profile, MigrationProfile) or not isinstance(
            self.grade, EvidenceGrade
        ):
            raise ContractError("Typed qualification profile and grade required")
        object.__setattr__(self, "operations", _names(self.operations))
        _row_count(self.max_rows)
        object.__setattr__(self, "issued_at", utc(self.issued_at))
        object.__setattr__(self, "expires_at", utc(self.expires_at))
        if self.issued_at >= self.expires_at:
            raise ContractError("Positive qualification lifetime required")

    @property
    def fingerprint(self):
        value = asdict(self)
        value["operations"] = list(self.operations)
        value["profile"] = str(self.profile)
        value["grade"] = str(self.grade)
        value["issued_at"] = self.issued_at.isoformat()
        value["expires_at"] = self.expires_at.isoformat()
        return _fingerprint(value)


def assess_strategy(
    contract,
    *,
    expected_tenant,
    source,
    target,
    manifest,
    qualification,
    required_operations,
    rows,
    authenticate,
    clock,
    required_grade=EvidenceGrade.LIVE,
):
    """Evaluate just C3 at a trusted caller's scope, never the whole contract.

    ``authenticate(record)`` must verify the entire record's current signature,
    issuer trust, raw evidence, registration, scope, and revocation. Only exact
    True is accepted. A user-provided approval flag is not such an authority.
    Downstream execution must recheck using the existing T10/security guards.
    """
    if not isinstance(contract, ServiceContract):
        raise ContractError("Typed service contract required")
    contract.inspect(expected_tenant=expected_tenant)
    if not isinstance(source, EnvironmentProfile) or not isinstance(
        target, EnvironmentProfile
    ):
        raise ContractError("Typed environment profiles required")
    if not isinstance(manifest, CapabilityManifest):
        raise ContractError("Typed capability manifest required")
    if not isinstance(required_grade, EvidenceGrade) or not callable(clock):
        raise ContractError("Trusted grade policy and clock required")
    operations = _names(required_operations)
    _row_count(rows)
    observed_at = utc(clock())

    def result(status, reason):
        return {
            "schema_version": 1,
            "status": status,
            "reason": reason,
            "contract_digest": contract.fingerprint,
            "manifest_digest": manifest.fingerprint,
            "source_profile_digest": source.fingerprint,
            "target_profile_digest": target.fingerprint,
            "qualification_digest": qualification.fingerprint
            if isinstance(qualification, StrategyQualification)
            else None,
            "evaluated_at": observed_at.isoformat(),
            "evaluated_scope": "C3_DIRECTIONAL_STRATEGY_ELIGIBILITY_ONLY",
            "contract_satisfied": False,
            "executable": False,
            "authorizes_execution": False,
        }

    if EXTENSIONS & set(contract.document["spec"]):
        return result("NEEDS_EVIDENCE", "CONTRACT_EXTENSION_NOT_EVALUATED")
    if contract.profile not in manifest.profiles:
        return result("UNSUPPORTED", "CAPABILITY_PROFILE_UNSUPPORTED")
    if not set(operations) <= set(manifest.operations):
        return result("UNSUPPORTED", "CAPABILITY_OPERATION_UNSUPPORTED")
    if not isinstance(qualification, StrategyQualification):
        return result("NEEDS_EVIDENCE", "QUALIFICATION_REQUIRED")
    if (
        qualification.tenant_id,
        qualification.service_id,
        qualification.contract_digest,
    ) != (expected_tenant, contract.service_id, contract.fingerprint):
        return result("NEEDS_EVIDENCE", "QUALIFICATION_SCOPE_MISMATCH")
    if (
        qualification.manifest_digest,
        qualification.source_profile_digest,
        qualification.target_profile_digest,
        qualification.profile,
    ) != (
        manifest.fingerprint,
        source.fingerprint,
        target.fingerprint,
        contract.profile,
    ):
        return result("NEEDS_EVIDENCE", "QUALIFICATION_BINDING_MISMATCH")
    if qualification.grade != required_grade:
        return result("NEEDS_EVIDENCE", "QUALIFICATION_GRADE_MISMATCH")
    if (
        not set(operations) <= set(qualification.operations)
        or rows > qualification.max_rows
    ):
        return result("NEEDS_EVIDENCE", "QUALIFICATION_LIMIT_EXCEEDED")
    if not qualification.issued_at <= observed_at < qualification.expires_at:
        return result("NEEDS_EVIDENCE", "QUALIFICATION_NOT_CURRENT")
    if not callable(authenticate):
        return result("NEEDS_EVIDENCE", "QUALIFICATION_AUTHENTICATION_REQUIRED")
    try:
        authenticated = authenticate(qualification)
    except Exception:
        return result("NEEDS_EVIDENCE", "QUALIFICATION_AUTHENTICATION_FAILED")
    if authenticated is not True:
        return result("NEEDS_EVIDENCE", "QUALIFICATION_AUTHENTICATION_FAILED")
    confirmed_at = utc(clock())
    if confirmed_at < observed_at:
        return result("NEEDS_EVIDENCE", "CLOCK_REGRESSION")
    observed_at = confirmed_at
    if not qualification.issued_at <= confirmed_at < qualification.expires_at:
        return result("NEEDS_EVIDENCE", "QUALIFICATION_NOT_CURRENT")
    return result("QUALIFIED_CANDIDATE", "DIRECTIONAL_QUALIFICATION_CURRENT")
