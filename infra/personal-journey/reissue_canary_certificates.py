"""Repair certificate extensions in a NEW private input revision.

Preserves existing private keys, passwords, immutable workload scope and image
digest. Default mode preserves expiration. Explicit --renew-until creates a new
bounded qualification window; old credentials are not made valid again.
No live changes or TLS relaxation are performed by this file generator.
"""

import argparse
import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--renew-until", type=datetime.fromisoformat,
                   help="Explicit new qualification window, future and at most eight hours")
    args = p.parse_args()
    os.umask(0o077)
    identity = json.loads((args.inputs / "identity.json").read_text())
    now, expires = datetime.now(UTC), datetime.fromisoformat(identity["expires_at"])
    previous_expiration = expires
    if identity["run_id"] != "b0be270e62":
        raise ValueError("Wrong personal run")
    if args.renew_until is None:
        if not now < expires:
            raise ValueError("Expired run requires an explicit new qualification window")
    else:
        expires = args.renew_until
        if expires.tzinfo is None or not now < expires <= now + timedelta(hours=8) or expires <= previous_expiration:
            raise ValueError("New qualification deadline must be timezone-aware, later, and within eight hours")
        identity["expires_at"] = expires.isoformat()
        identity["previous_expires_at"] = previous_expiration.isoformat()
        identity["qualification_window_renewed_at"] = now.isoformat()
    if args.output.exists() or any(path.is_symlink() for path in args.inputs.rglob("*")):
        raise ValueError("New private output and regular original inputs required")
    old = x509.load_pem_x509_certificate((args.inputs / "pki/ca.pem").read_bytes())
    key = serialization.load_pem_private_key((args.inputs / "pki/ca-key.pem").read_bytes(), password=None)
    if old.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise ValueError("CA key does not match existing trust bootstrap")
    ca = (x509.CertificateBuilder().subject_name(old.subject).issuer_name(old.subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256()))
    shutil.copytree(args.inputs, args.output)
    pem = ca.public_bytes(serialization.Encoding.PEM)
    for directory in (args.output / "pki", args.output / "client"):
        (directory / "ca.pem").write_bytes(pem)
    for side in ("source", "target"):
        for kind in ("db", "app"):
            directory = args.output / (side + "-" + kind)
            previous = x509.load_pem_x509_certificate((directory / "server.pem").read_bytes())
            leafkey = serialization.load_pem_private_key((directory / "server-key.pem").read_bytes(), password=None)
            if previous.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != leafkey.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
                raise ValueError("Leaf key differs from previous deployment inputs")
            cert = (x509.CertificateBuilder().subject_name(previous.subject).issuer_name(ca.subject)
                .public_key(leafkey.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expires)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName(previous.extensions.get_extension_for_class(x509.SubjectAlternativeName).value), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(leafkey.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
                .sign(key, hashes.SHA256()))
            (directory / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (directory / "ca.pem").write_bytes(pem)
            if kind == "app":
                (directory / "database-ca.pem").write_bytes(pem)
                config = json.loads((directory / "config.json").read_text())
                if config["run_id"] != identity["run_id"]:
                    raise ValueError("Application scope changed")
                config["expires_at"] = expires.isoformat()
                (directory / "config.json").write_text(json.dumps(config))
            identity["environments"][side]["database_certificate_sha256" if kind == "db" else "application_certificate_sha256"] = cert.fingerprint(hashes.SHA256()).hex()
    payload = {kind: {file.name: base64.b64encode(file.read_bytes()).decode()
        for file in sorted((args.output / ("source-" + kind)).iterdir())} for kind in ("db", "app")}
    (args.output / "source-secret.json").write_text(json.dumps({"version": 1, "run_id": identity["run_id"], "files": payload}))
    identity["ca_certificate_sha256"] = ca.fingerprint(hashes.SHA256()).hex()
    (args.output / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    summary = {"run_id": identity["run_id"], "old_ca": old.fingerprint(hashes.SHA256()).hex(),
        "new_ca": identity["ca_certificate_sha256"], "private_keys_preserved": True,
        "passwords_preserved": True, "expires_at": expires.isoformat(),
        "previous_expires_at": previous_expiration.isoformat(),
        "explicit_new_qualification_window": args.renew_until is not None,
        "expiration_preserved": args.renew_until is None,
        "added_extensions": ["SubjectKeyIdentifier", "AuthorityKeyIdentifier"], "deployed": False}
    (args.output / "certificate-revision.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
