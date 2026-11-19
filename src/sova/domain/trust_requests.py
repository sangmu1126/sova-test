"""Short-lived, app-scoped verifier-key requests; never a caller-supplied approval."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib

from sova.domain.contracts import ContractError, ResourceScope, canonical_json, identifier, utc
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import trust_document


TRUST_FIELDS = {"issuer_id", "key_id", "public_key_hex", "allowed_scopes", "allowed_verifiers",
                "valid_from", "valid_until", "revoked"}


def decode_trust(document):
    try:
        if type(document) is not dict or set(document) != TRUST_FIELDS:
            raise ContractError("Exact public verifier trust fields required")
        if type(document["allowed_scopes"]) is not list or type(document["allowed_verifiers"]) is not list:
            raise ContractError("Bounded public trust scope/version lists required")
        scopes = []
        for value in document["allowed_scopes"]:
            if type(value) is not dict or set(value) != {"tenant_id", "application_id", "environment_id", "resource_id"}:
                raise ContractError("Exact verifier scope fields required")
            scopes.append(ResourceScope(**value))
        return VerifierTrust(document["issuer_id"], document["key_id"], document["public_key_hex"],
            tuple(scopes), tuple(document["allowed_verifiers"]), datetime.fromisoformat(document["valid_from"]),
            datetime.fromisoformat(document["valid_until"]), document["revoked"])
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError("Invalid public verifier trust") from error


@dataclass(frozen=True, slots=True)
class VerifierTrustRequest:
    tenant_id: str
    application_id: str
    request_id: str
    expected_revision: int
    trust: VerifierTrust
    requested_by: str
    reason: str
    requested_at: datetime
    expires_at: datetime

    def __post_init__(self):
        for key in ("tenant_id", "application_id", "request_id", "requested_by", "reason"):
            identifier(getattr(self, key), key, limit=1024 if key == "reason" else 256)
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ContractError("Expected trust revision required (0 for initial publication)")
        if not isinstance(self.trust, VerifierTrust) or any(
            (s.tenant_id, s.application_id) != (self.tenant_id, self.application_id)
            for s in self.trust.allowed_scopes):
            raise ContractError("Verifier request cannot cross tenant/application")
        object.__setattr__(self, "requested_at", utc(self.requested_at))
        object.__setattr__(self, "expires_at", utc(self.expires_at))
        if not self.requested_at < self.expires_at <= self.requested_at + timedelta(minutes=15):
            raise ContractError("Verifier request requires a bounded approval window")
        if self.trust.valid_until > self.requested_at + timedelta(hours=8):
            raise ContractError("Verifier grants are limited to an explicit eight-hour lease")
        if not self.trust.revoked and self.trust.valid_until <= self.requested_at:
            raise ContractError("New verifier grant already expired")

    @property
    def document(self):
        return {"schema_version": 1, "tenant_id": self.tenant_id, "application_id": self.application_id,
            "request_id": self.request_id, "expected_revision": self.expected_revision,
            "trust": trust_document(self.trust), "requested_by": self.requested_by, "reason": self.reason,
            "requested_at": self.requested_at.isoformat(), "expires_at": self.expires_at.isoformat()}

    @property
    def fingerprint(self):
        return hashlib.sha256(canonical_json(self.document, limit=131072).encode()).hexdigest()

    def require_current(self, now):
        if not self.requested_at <= utc(now) < self.expires_at:
            raise ContractError("Verifier request expired or future")
        if not self.trust.revoked and utc(now) >= self.trust.valid_until:
            raise ContractError("Proposed verifier lease expired")

    @classmethod
    def from_document(cls, document):
        expected = {"schema_version", "tenant_id", "application_id", "request_id", "expected_revision", "trust",
                    "requested_by", "reason", "requested_at", "expires_at"}
        try:
            if type(document) is not dict or set(document) != expected or type(document["schema_version"]) is not int or document["schema_version"] != 1:
                raise ContractError("Invalid verifier request document")
            return cls(document["tenant_id"], document["application_id"], document["request_id"], document["expected_revision"],
                decode_trust(document["trust"]), document["requested_by"], document["reason"],
                datetime.fromisoformat(document["requested_at"]), datetime.fromisoformat(document["expires_at"]))
        except (KeyError, ValueError, TypeError) as error:
            raise ContractError("Invalid verifier request document") from error


def new_request(*, tenant, application, request_id, document, actor, now):
    if type(document) is not dict or set(document) != {"expected_revision", "trust", "reason"}:
        raise ContractError("Body is {expected_revision, trust, reason}; actor and time are server-derived")
    now = utc(now)
    trust = decode_trust(document["trust"])
    return VerifierTrustRequest(tenant, application, request_id, document["expected_revision"], trust,
                               actor, document["reason"], now, now + timedelta(minutes=15))
