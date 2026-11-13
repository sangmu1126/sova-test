"""Bounded, immutable requirements; normalization is never registration or approval.

The alpha format preserves all accepted fields in its digest. Identity/incident
extensions are retained but explicitly unevaluated by this first compiler stage.
No runtime observation is inferred from a service owner's requirements.
"""

from dataclasses import dataclass
from enum import StrEnum
import hashlib

from sova.domain.json_documents import json_document
from sova.domain.contracts import ContractError, canonical_json, identifier


MAX_CONTRACT_BYTES = 65536
MAX_SAFE_INTEGER = 2**53 - 1


class MigrationProfile(StrEnum):
    OBSERVE = "OBSERVE"
    PREPARE = "PREPARE"
    SINGLE_AUTHORITY = "PLANNED_SINGLE_AUTHORITY"
    PARTITIONED = "PARTITIONED_HANDOFF"
    REVERSE = "REVERSE_MIGRATION"
    FAILBACK = "FAILBACK"
    DISASTER_RECOVERY = "DISASTER_RECOVERY"


EXTENSIONS = frozenset(
    (
        "identity",
        "security_hold_policy",
        "evidence_freshness",
        "incident_recovery_objectives",
    )
)


def _object(value, required, optional=()):
    if type(value) is not dict or not set(required) <= set(value) <= set(
        required
    ) | set(optional):
        raise ContractError("Service contract fields do not match the schema")


def _text(value, *, limit=256):
    return identifier(value, "service contract value", limit=limit)


def _boolean(value):
    if type(value) is not bool:
        raise ContractError("Service contract boolean required")


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise ContractError("Bounded service contract integer required")


def _texts(value, *, minimum=1):
    if type(value) is not list or not minimum <= len(value) <= 256:
        raise ContractError("Bounded service contract list required")
    for item in value:
        _text(item)
    if len(value) != len(set(value)):
        raise ContractError("Duplicate service contract item")


def _records(value, fields, *, minimum=0):
    if type(value) is not list or not minimum <= len(value) <= 256:
        raise ContractError("Bounded service contract records required")
    ids = set()
    for item in value:
        _object(item, fields)
        for key in fields:
            _text(item[key], limit=2048 if key == "description" else 256)
        if item["id"] in ids:
            raise ContractError("Duplicate service contract record")
        ids.add(item["id"])


def _json_numbers(value):
    # Alpha canonical encoding is deliberately narrower than arbitrary JSON/JCS.
    # Large precise quantities are strings, never rounded floating-point values.
    if type(value) is float:
        raise ContractError("Floating point quantities are not supported")
    if type(value) is int:
        _integer(value)
    elif type(value) is dict:
        for item in value.values():
            _json_numbers(item)
    elif type(value) is list:
        for item in value:
            _json_numbers(item)


def _validate(data):
    _object(data, ("apiVersion", "kind", "metadata", "spec"))
    if (
        data["apiVersion"] != "migration.platform/v1alpha1"
        or data["kind"] != "ServiceContract"
    ):
        raise ContractError("Unsupported service contract version or kind")
    metadata, spec = data["metadata"], data["spec"]
    _object(metadata, ("tenant_id", "id", "revision"))
    _text(metadata["tenant_id"])
    _text(metadata["id"])
    _integer(metadata["revision"], 1)
    _object(
        spec,
        (
            "owner",
            "acknowledgment_semantics",
            "business",
            "migration",
            "data",
            "availability",
            "security",
            "residency",
            "operations",
            "approval",
            "exit",
        ),
        EXTENSIONS,
    )
    _text(spec["owner"])
    ack = spec["acknowledgment_semantics"]
    _object(ack, ("accepted", "completed"))
    _text(ack["accepted"])
    _text(ack["completed"])
    business = spec["business"]
    _object(business, ("invariants", "external_effects"))
    _records(business["invariants"], ("id", "description"), minimum=1)
    _records(
        business["external_effects"],
        ("id", "idempotency", "result_lookup", "on_unknown"),
    )
    migration = spec["migration"]
    _object(migration, ("profile", "units"))
    try:
        MigrationProfile(migration["profile"])
    except (TypeError, ValueError) as exc:
        raise ContractError("Unsupported migration profile") from exc
    units = migration["units"]
    if type(units) is not dict:
        raise ContractError("Migration unit contract required")
    if units.get("mode") == "whole_service":
        _object(units, ("mode",))
    elif units.get("mode") == "verified_partition":
        _object(
            units,
            (
                "mode",
                "partition_key",
                "partition_contract_ref",
                "reject_cross_partition_transactions",
            ),
        )
        _text(units["partition_key"])
        _text(units["partition_contract_ref"])
        if units["reject_cross_partition_transactions"] is not True:
            raise ContractError(
                "Verified partition must reject cross-partition transactions"
            )
    else:
        raise ContractError("Unsupported migration unit mode")
    data_rules = spec["data"]
    _object(
        data_rules,
        ("protected_sets", "preserve_completed_operations", "comparison_contract_ref"),
    )
    _texts(data_rules["protected_sets"])
    _boolean(data_rules["preserve_completed_operations"])
    _text(data_rules["comparison_contract_ref"])
    availability = spec["availability"]
    _object(
        availability,
        (
            "max_write_admission_pause_ms",
            "max_client_completion_wait_ms",
            "latency_budget_ref",
            "read_consistency_during_handoff",
        ),
    )
    for key in ("max_write_admission_pause_ms", "max_client_completion_wait_ms"):
        _integer(availability[key])
    _text(availability["latency_budget_ref"])
    _text(availability["read_consistency_during_handoff"])
    _object(spec["security"], ("required_controls",))
    _texts(spec["security"]["required_controls"])
    _object(spec["residency"], ("business_data", "backups", "audit", "ai_inputs"))
    for key, value in spec["residency"].items():
        _texts(value, minimum=0 if key == "ai_inputs" else 1)
    _object(
        spec["operations"],
        ("restore_test_required", "source_retirement_requires_separate_approval"),
    )
    for value in spec["operations"].values():
        _boolean(value)
    _object(spec["approval"], ("separation_of_duties", "high_risk_handoff"))
    _boolean(spec["approval"]["separation_of_duties"])
    _text(spec["approval"]["high_risk_handoff"])
    _object(spec["exit"], ("remove", "allowed_remaining"))
    _texts(spec["exit"]["remove"], minimum=0)
    _texts(spec["exit"]["allowed_remaining"], minimum=0)
    if set(spec["exit"]["remove"]) & set(spec["exit"]["allowed_remaining"]):
        raise ContractError("An exit dependency cannot be both removed and retained")
    for extension in EXTENSIONS & set(spec):
        if type(spec[extension]) is not dict or not spec[extension]:
            raise ContractError("Non-empty extension contract required")
    _json_numbers(data)


@dataclass(frozen=True, slots=True)
class ServiceContract:
    canonical: str

    def __post_init__(self):
        data = json_document(self.canonical, limit=MAX_CONTRACT_BYTES)
        _validate(data)
        object.__setattr__(
            self, "canonical", canonical_json(data, limit=MAX_CONTRACT_BYTES)
        )

    @classmethod
    def from_document(cls, data):
        return cls(canonical_json(data, limit=MAX_CONTRACT_BYTES))

    @property
    def document(self):
        # A fresh copy prevents caller mutation from changing the frozen contract.
        return json_document(self.canonical, limit=MAX_CONTRACT_BYTES)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.canonical.encode()).hexdigest()

    @property
    def tenant_id(self):
        return self.document["metadata"]["tenant_id"]

    @property
    def service_id(self):
        return self.document["metadata"]["id"]

    @property
    def profile(self):
        return MigrationProfile(self.document["spec"]["migration"]["profile"])

    def inspect(self, *, expected_tenant):
        _text(expected_tenant)
        if expected_tenant != self.tenant_id:
            raise ContractError("Service contract tenant scope mismatch")
        data = self.document
        return {
            "schema_version": 1,
            "status": "NORMALIZED",
            "tenant_id": self.tenant_id,
            "service_id": self.service_id,
            "revision": data["metadata"]["revision"],
            "contract_digest": self.fingerprint,
            "profile": str(self.profile),
            "required_controls": data["spec"]["security"]["required_controls"],
            "protected_sets": data["spec"]["data"]["protected_sets"],
            "unevaluated_extensions": sorted(EXTENSIONS & set(data["spec"])),
            "registered": False,
            "executable": False,
            "live_verified": False,
            "blockers": [
                "AUTHENTICATED_REGISTRATION_REQUIRED",
                "SEMANTIC_AND_DEPENDENCY_ANALYSIS_REQUIRED",
                "DIRECTIONAL_QUALIFICATION_REQUIRED",
                "APPROVAL_REQUIRED",
            ],
        }
