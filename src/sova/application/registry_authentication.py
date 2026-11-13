"""Authenticate registry documents through existing signed security evidence.

The host supplies an exact-digest envelope lookup and current verifier trust
resolution (``PostgresSignedEvidence.find_envelope`` and
``PostgresVerifierRegistry.resolve`` in production). Every call re-reads the
envelope and trust after verification so a removed record, a revoked key, or
an expiry during the check cannot leave a stale True. Nothing is cached and the
result is only ever exact ``True``; any failure raises a sanitized error that
callers (``assess_strategy``, ``analyze_boundary``) treat as not authenticated.
"""

from sova.adapters.ed25519_evidence import Ed25519EvidenceVerifier
from sova.domain.contracts import ContractError, identifier, utc
from sova.domain.registry_attestation import registry_binding
from sova.domain.signed_evidence import EvidenceEnvelope, VerifierTrust


class RegistryAuthenticator:
    def __init__(self, *, find_envelope, resolve_trust, clock, verifier_version):
        if not all(callable(f) for f in (find_envelope, resolve_trust, clock)):
            raise ContractError("Registry lookup, trust, and clock callbacks required")
        self.find_envelope = find_envelope
        self.resolve_trust = resolve_trust
        self.clock = clock
        self.verifier_version = identifier(verifier_version)
        self.verifier = Ed25519EvidenceVerifier(resolve_trust)

    def _lookup(self, binding):
        try:
            envelope = self.find_envelope(
                binding.scope,
                artifact_digest=binding.artifact_digest,
                verifier_version=self.verifier_version,
            )
        except Exception as exc:
            # Missing, ambiguous (re-attested/revoked), or unreachable all fail closed.
            raise ContractError("Registry attestation unavailable") from exc
        if not isinstance(envelope, EvidenceEnvelope) or not binding.matches(
            envelope.evidence
        ):
            raise ContractError("Registry attestation binding mismatch")
        if envelope.evidence.verifier_version != self.verifier_version:
            raise ContractError("Registry attestation verifier mismatch")
        return envelope

    def _verify(self, envelope, now):
        try:
            trust = self.resolve_trust(envelope.issuer_id, envelope.key_id)
            if not isinstance(trust, VerifierTrust):
                raise ContractError("Current registry verifier trust required")
            self.verifier.verify(envelope, expected_record=envelope.evidence, now=now)
        except ContractError:
            raise
        except Exception as exc:
            # Store/network errors may carry connection details; never surface them.
            raise ContractError("Registry verifier trust unavailable") from exc
        return trust

    def __call__(self, subject):
        binding = registry_binding(subject)
        started = utc(self.clock())
        envelope = self._lookup(binding)
        trust = self._verify(envelope, started)
        # Re-read original state: the record, trust, and time must be unchanged.
        if self._lookup(binding) != envelope:
            raise ContractError("Registry attestation changed during verification")
        finished = utc(self.clock())
        if finished < started:
            raise ContractError("Registry clock regression")
        if self._verify(envelope, finished) != trust:
            raise ContractError("Registry verifier trust changed during verification")
        return True


def postgres_registry_authenticator(
    connect, *, tenant_id, application_id, verifier_version, clock
):
    """Production wiring over the existing security stores; no new tables."""
    from sova.adapters.postgres_signed_evidence import PostgresSignedEvidence
    from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry

    signed = PostgresSignedEvidence(connect, tenant_id=tenant_id)
    registry = PostgresVerifierRegistry(
        connect, tenant_id=tenant_id, application_id=application_id
    )
    return RegistryAuthenticator(
        find_envelope=signed.find_envelope,
        resolve_trust=registry.resolve,
        clock=clock,
        verifier_version=verifier_version,
    )
