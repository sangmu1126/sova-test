"""Registry documents in and out: one strict codec per registered kind.

A registration is accepted only as a JSON document that decodes to the typed
domain value and encodes back to exactly the same normalized document, so
what is stored is what the compiler reads. Unknown or missing fields are
refused. Decoding authenticates nothing: a decoded document still needs its
signed registry attestation before a plan may use it.
"""

from datetime import datetime

from sova.adapters.registration.dependency_codec import decode_dependency_snapshot
from sova.domain.capability_catalog import (
    CapabilityManifest,
    EnvironmentProfile,
    EvidenceGrade,
    StrategyQualification,
)
from sova.domain.contracts import ContractError, utc
from sova.domain.migration_units import DependencySnapshot
from sova.domain.service_contracts import MigrationProfile, ServiceContract

KINDS = ("service_contract", "dependency_snapshot", "environment_profile",
         "capability_manifest", "strategy_qualification")


def _exact(data, required, optional=()):
    if type(data) is not dict:
        raise ContractError("A JSON object is required")
    keys = set(data)
    if not set(required) <= keys or keys - set(required) - set(optional):
        raise ContractError("Document fields do not match the registered schema")


def _schema(data):
    if data.get("schema_version") != 1:
        raise ContractError("Unsupported document schema version")


def _profile_doc(p):
    return {"schema_version": 1, "profile_id": p.profile_id, "revision": p.revision,
            "provider": p.provider, "compute_backend": p.compute_backend,
            "data_engine": p.data_engine, "identity_mechanism": p.identity_mechanism,
            "network_topology": p.network_topology, "architecture": p.architecture,
            "versions": dict(p.versions)}


def _decode_profile(d):
    _exact(d, ("schema_version", "profile_id", "revision", "provider", "compute_backend",
               "data_engine", "identity_mechanism", "network_topology", "architecture",
               "versions"))
    _schema(d)
    if type(d["versions"]) is not dict:
        raise ContractError("Versions must be a mapping")
    return EnvironmentProfile(
        d["profile_id"], d["revision"], d["provider"], d["compute_backend"], d["data_engine"],
        d["identity_mechanism"], d["network_topology"], d["architecture"],
        tuple(d["versions"].items()))


def _manifest_doc(m):
    return {"schema_version": 1, "adapter_id": m.adapter_id,
            "adapter_version": m.adapter_version, "adapter_digest": m.adapter_digest,
            "strategy_id": m.strategy_id, "strategy_version": m.strategy_version,
            "operations": list(m.operations), "profiles": [str(p) for p in m.profiles],
            **({"properties": list(m.properties)} if m.properties else {})}


def _decode_manifest(d):
    _exact(d, ("schema_version", "adapter_id", "adapter_version", "adapter_digest",
               "strategy_id", "strategy_version", "operations", "profiles"), ("properties",))
    _schema(d)
    try:
        profiles = tuple(MigrationProfile(p) for p in d["profiles"])
    except (TypeError, ValueError):
        raise ContractError("Unknown migration profile") from None
    return CapabilityManifest(
        d["adapter_id"], d["adapter_version"], d["adapter_digest"], d["strategy_id"],
        d["strategy_version"], tuple(d["operations"]), profiles,
        tuple(d.get("properties", ())))


def _qualification_doc(q):
    return {"schema_version": 1, "record_id": q.record_id, "tenant_id": q.tenant_id,
            "service_id": q.service_id, "contract_digest": q.contract_digest,
            "manifest_digest": q.manifest_digest,
            "source_profile_digest": q.source_profile_digest,
            "target_profile_digest": q.target_profile_digest, "profile": str(q.profile),
            "operations": list(q.operations), "max_rows": q.max_rows,
            "issued_at": q.issued_at.isoformat(), "expires_at": q.expires_at.isoformat(),
            "verifier_identity": q.verifier_identity, "verifier_digest": q.verifier_digest,
            "test_definition_digest": q.test_definition_digest,
            "raw_evidence_digest": q.raw_evidence_digest, "grade": str(q.grade),
            **({"properties": list(q.properties)} if q.properties else {})}


def _time(value):
    if type(value) is not str:
        raise ContractError("ISO 8601 time required")
    try:
        return utc(datetime.fromisoformat(value))
    except ValueError:
        raise ContractError("ISO 8601 time required") from None


def _decode_qualification(d):
    _exact(d, ("schema_version", "record_id", "tenant_id", "service_id", "contract_digest",
               "manifest_digest", "source_profile_digest", "target_profile_digest",
               "profile", "operations", "max_rows", "issued_at", "expires_at",
               "verifier_identity", "verifier_digest", "test_definition_digest",
               "raw_evidence_digest", "grade"), ("properties",))
    _schema(d)
    try:
        profile, grade = MigrationProfile(d["profile"]), EvidenceGrade(d["grade"])
    except (TypeError, ValueError):
        raise ContractError("Unknown profile or evidence grade") from None
    return StrategyQualification(
        d["record_id"], d["tenant_id"], d["service_id"], d["contract_digest"],
        d["manifest_digest"], d["source_profile_digest"], d["target_profile_digest"],
        profile, tuple(d["operations"]), d["max_rows"], _time(d["issued_at"]),
        _time(d["expires_at"]), d["verifier_identity"], d["verifier_digest"],
        d["test_definition_digest"], d["raw_evidence_digest"], grade,
        tuple(d.get("properties", ())))


_DECODE = {"service_contract": ServiceContract.from_document,
           "dependency_snapshot": decode_dependency_snapshot,
           "environment_profile": _decode_profile,
           "capability_manifest": _decode_manifest,
           "strategy_qualification": _decode_qualification}


def encode(value):
    """The normalized document of a typed registry value."""
    if isinstance(value, (ServiceContract, DependencySnapshot)):
        return value.document
    if isinstance(value, EnvironmentProfile):
        return _profile_doc(value)
    if isinstance(value, CapabilityManifest):
        return _manifest_doc(value)
    if isinstance(value, StrategyQualification):
        return _qualification_doc(value)
    raise ContractError("Not a registry value")


def kind_of(value):
    return {ServiceContract: "service_contract", DependencySnapshot: "dependency_snapshot",
            EnvironmentProfile: "environment_profile",
            CapabilityManifest: "capability_manifest",
            StrategyQualification: "strategy_qualification"}[type(value)]


def subject_of(value):
    """The natural identity a registration of ``value`` is filed under."""
    if isinstance(value, ServiceContract):
        return value.service_id
    if isinstance(value, DependencySnapshot):
        return value.snapshot_id
    if isinstance(value, EnvironmentProfile):
        return value.profile_id
    if isinstance(value, CapabilityManifest):
        return f"{value.adapter_id}.{value.strategy_id}"
    return value.record_id


def tenant_of(value):
    """The tenant a document names, or None for catalog entries (profiles, manifests)."""
    if isinstance(value, ServiceContract):
        return value.tenant_id
    if isinstance(value, DependencySnapshot):
        return value.graph.tenant_id
    if isinstance(value, StrategyQualification):
        return value.tenant_id
    return None


def decode(kind, document):
    """Typed value of a document of ``kind``; refused unless it round-trips exactly."""
    if kind not in _DECODE:
        raise ContractError("Unknown registry kind")
    value = _DECODE[kind](document)
    if encode(value) != document:
        raise ContractError("Document is not in its normalized form")
    return value
