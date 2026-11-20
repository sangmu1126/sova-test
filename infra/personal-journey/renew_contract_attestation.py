"""Propose a new revision of the same personal service requirements.

Never replay a second envelope for an unchanged digest, extend old evidence,
write directly to the registry, or issue execution approval. The native UI/API
must CAS-register the revision and independently verify the scoped signature.
This operator-only helper reads the existing contract and approved Keychain
identity; it is not an uploaded-document or agent tool.
"""

import argparse
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import psycopg

from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.adapters.postgres_registrations import PostgresRegistrations
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.adapters.registration.registry_codec import encode
from sova.domain.contracts import canonical_json
from sova.domain.registry_attestation import attestation_record
from sova.domain.service_contracts import ServiceContract
from sova.service.directional_catalog import source_identity


def next_contract(document, tenant, application):
    current = ServiceContract.from_document(document)
    value = deepcopy(current.document)
    metadata = value["metadata"]
    if metadata["tenant_id"] != tenant or metadata["id"] != application:
        raise ValueError("Existing personal contract scope differs")
    metadata["revision"] += 1
    renewed = ServiceContract.from_document(value)
    if (
        renewed.document["spec"] != current.document["spec"]
        or renewed.fingerprint == current.fingerprint
    ):
        raise ValueError(
            "Renewal must preserve requirements and change the revision digest"
        )
    return renewed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "control-directory", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    if a.output.exists():
        raise ValueError("Never overwrite a previous renewal proposal")
    identity = json.loads((a.inputs / "identity.json").read_text())
    now, end = datetime.now(UTC), datetime.fromisoformat(identity["expires_at"])
    if identity["run_id"] != "b0be270e62" or not now < end:
        raise ValueError("Current owned trial required")

    def connect():
        return psycopg.connect(
            (a.control_directory / "runtime.dsn").read_text(), connect_timeout=5
        )

    registry = PostgresRegistrations(connect, tenant_id=identity["tenant_id"])
    old = registry.current("service_contract", identity["application_id"])
    if old is None:
        raise ValueError(
            "Existing contract required; never bootstrap substitute requirements"
        )
    contract = next_contract(
        old.document, identity["tenant_id"], identity["application_id"]
    )
    trust = PostgresVerifierRegistry(
        connect,
        tenant_id=identity["tenant_id"],
        application_id=identity["application_id"],
    ).resolve("personal-registry-verifier-b0be270e62", "registry-v3")
    record = attestation_record(
        contract,
        verifier_version="registry-attestor-1",
        observed_at=now,
        valid_until=end,
    )
    if (
        trust.revoked
        or not trust.valid_from <= now < end <= trust.valid_until
        or "registry-attestor-1" not in trust.allowed_verifiers
        or record.scope not in trust.allowed_scopes
    ):
        raise ValueError("Current independently approved verifier trust required")
    sys.path.insert(
        0,
        "/Users/Sooming_/dev/softbank_2026/personal-development/.local-deps/personal-keychain",
    )
    from keyring.backends.macOS import Keyring

    private = Keyring().get_password(
        "sova.personal.registry.b0be270e62", "registry-producer-v3"
    )
    if private is None:
        raise ValueError("Never create a substitute signing identity")
    signer = Ed25519EvidenceSigner(
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private)),
        issuer_id="personal-registry-verifier-b0be270e62",
        key_id="registry-v3",
    )
    if signer.public_key_hex != trust.public_key_hex:
        raise ValueError("Keychain identity differs from approved verifier")
    package = {
        "version": 1,
        "run_id": identity["run_id"],
        "observed_at": now.isoformat(),
        "expires_at": end.isoformat(),
        "implementation_digest": source_identity()["digest"],
        "registrations": [
            {
                "kind": "service_contract",
                "subject": identity["application_id"],
                "expected_revision": old.revision,
                "document": encode(contract),
            }
        ],
        "attestations": [signer.sign(record, issued_at=now).document],
        "previous_contract_digest": old.digest,
        "requirements_changed": False,
        "expired_evidence_reused": False,
        "business_plan_admitted": False,
    }
    a.output.write_text(canonical_json(package, limit=1 << 20) + "\n")
    print(
        json.dumps(
            {
                "proposed_contract_revision": contract.document["metadata"]["revision"],
                "registered": False,
                "requirements_changed": False,
                "execution_approved": False,
            }
        )
    )


if __name__ == "__main__":
    main()
