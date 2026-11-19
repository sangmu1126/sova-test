"""Own canary verifier key in macOS Keychain; export only its public proposal."""

from datetime import UTC, datetime
import sys
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.domain.contracts import ResourceScope
from sova.domain.registry_attestation import REGISTRY_ENVIRONMENT, RegistryKind
from sova.domain.signed_evidence import VerifierTrust
from sova.domain.verifier_registry import trust_document
from sova.ports.plans import RecordNotFound


SERVICE = "sova.personal.registry.b0be270e62"
ACCOUNT = "registry-producer-v3"


def registry_proposal(inputs, connect):
    sys.path.insert(0, "/Users/Sooming_/dev/softbank_2026/personal-development/.local-deps/personal-keychain")
    from keyring.backends.macOS import Keyring

    if inputs["run_id"] != "b0be270e62" or datetime.now(UTC) >= datetime.fromisoformat(inputs["expires_at"]):
        raise ValueError("Unexpired owned qualification scope required")
    ring = Keyring()  # No fallback backend, unrelated credentials or credential listing.
    value = ring.get_password(SERVICE, ACCOUNT)
    if value is None:
        key = Ed25519PrivateKey.generate()
        value = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                  serialization.NoEncryption()).hex()
        ring.set_password(SERVICE, ACCOUNT, value)
        if ring.get_password(SERVICE, ACCOUNT) != value:
            raise ValueError("Private verifier key Keychain readback failed")
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(value))
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    registry = PostgresVerifierRegistry(connect, tenant_id=inputs["tenant_id"], application_id=inputs["application_id"])
    expected = 0
    try:
        previous = registry.latest("personal-registry-verifier-b0be270e62", "registry-v3")
    except RecordNotFound:
        previous = None
    if previous is not None:
        if previous.trust.revoked or previous.trust.public_key_hex != public:
            raise ValueError("Refuse to replace or reactivate a different/revoked verifier key")
        expected = previous.revision
    now = datetime.now(UTC)
    trust = VerifierTrust("personal-registry-verifier-b0be270e62", "registry-v3", public,
        tuple(ResourceScope(inputs["tenant_id"], inputs["application_id"], REGISTRY_ENVIRONMENT, str(kind)) for kind in RegistryKind),
        ("registry-attestor-1",), now, datetime.fromisoformat(inputs["expires_at"]))
    return {"request_id": "registry-key-" + uuid4().hex,
        "document": {"expected_revision": expected, "trust": trust_document(trust),
            "reason": "Scoped personal AWS/VM qualification verifier; public key only; no execution approval"},
        "private_key_reference": "macos-keychain:" + SERVICE + "/" + ACCOUNT}
