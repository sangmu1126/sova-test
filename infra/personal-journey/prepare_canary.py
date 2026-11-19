"""Prepare private, run-scoped TLS/SCRAM inputs; never deploy or grant access.

The CA key stays on this Mac. Only each server's own leaf key and app-specific
credential enter its inputs. This disposable PKI is not enterprise issuance.
"""

import argparse
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from psycopg.conninfo import make_conninfo

from sova.qualification.order_schema import SCHEMA


def artifact(directory):
    image = json.loads((directory / "image.json").read_text())
    scan = json.loads((directory / "scan-summary.json").read_text())
    if (scan["preflight"] != "PASS" or any(scan["counts"].values())
            or image["manifest_digest"] != scan["manifest_digest"]
            or image["config_digest"] != scan["config_digest"]):
        raise ValueError("Exact all-severity-zero preparatory image scan required")
    with (directory / "canary.oci.tar").open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != image["archive_sha256"]:
            raise ValueError("Artifact changed after inspection")
    return image


def write(directory, name, value):
    path = directory / name
    with path.open("xb") as file:
        file.write(value.encode() if isinstance(value, str) else value)
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--app-image-directory", required=True, type=Path)
    parser.add_argument("--postgres-image-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    now, expires = datetime.now(UTC), datetime.fromisoformat(args.expires_at)
    if (not re.fullmatch(r"[a-z0-9]{8,12}", args.run_id) or expires.tzinfo is None
            or not now < expires <= now + timedelta(hours=8)):
        raise ValueError("Fresh run and explicit deadline within eight hours required")
    app_image, pg_image = artifact(args.app_image_directory), artifact(args.postgres_image_directory)
    if app_image["user"] != "10001:10001" or pg_image["user"] != "70:70":
        raise ValueError("Fixed non-root application and database images required")
    root = args.output.resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    pki = root / "pki"
    pki.mkdir(mode=0o700)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sova personal " + args.run_id)])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .sign(ca_key, hashes.SHA256()))
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    write(pki, "ca-key.pem", ca_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    write(pki, "ca.pem", ca_pem)

    def leaf(directory, host):
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder().subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, host)])).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expires)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .sign(ca_key, hashes.SHA256()))
        write(directory, "server.pem", cert.public_bytes(serialization.Encoding.PEM))
        write(directory, "server-key.pem", key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        write(directory, "ca.pem", ca_pem)
        return cert.fingerprint(hashes.SHA256()).hex()

    token = secrets.token_urlsafe(40)
    namespace = "sj-" + args.run_id
    summary = {"run_id": args.run_id, "tenant_id": "personal-qualification",
        "application_id": "orders-" + args.run_id, "namespace": namespace,
        "expires_at": expires.isoformat(), "app_manifest": app_image["manifest_digest"],
        "postgres_manifest": pg_image["manifest_digest"], "ca_certificate_sha256": ca.fingerprint(hashes.SHA256()).hex(),
        "environments": {}, "deployed": False, "operational_qualification": False}
    client = root / "client"
    client.mkdir(mode=0o700)
    write(client, "client.token", token)
    write(client, "ca.pem", ca_pem)
    for side in ("source", "target"):
        database = side + "_orders"
        db_host = ("source-db." + args.run_id + ".sova.invalid" if side == "source"
                   else "orders-db." + namespace + ".svc.cluster.local")
        app_host = side + "-app." + args.run_id + ".sova.invalid"
        directories = {}
        for kind in ("db", "app"):
            directory = root / (side + "-" + kind)
            directory.mkdir(mode=0o700)
            directories[kind] = directory
        admin, password, batch = [secrets.token_urlsafe(40) for _ in range(3)]
        db_cert = leaf(directories["db"], db_host)
        app_cert = leaf(directories["app"], app_host)
        write(directories["db"], "admin-password", admin)
        write(directories["db"], "pg_hba.conf", "local all postgres peer\nlocal all all reject\n"
            "hostnossl all all 0.0.0.0/0 reject\nhostnossl all all ::0/0 reject\n"
            "hostssl all all 0.0.0.0/0 scram-sha-256\nhostssl all all ::0/0 scram-sha-256\n")
        login = "LOGIN" if side == "source" else "NOLOGIN"
        # URL-safe generated passwords have no SQL metacharacters. They never
        # reach shell arguments, Terraform or public reports.
        ddl = (f"CREATE ROLE orders_app {login} PASSWORD '{password}';\n"
            f"CREATE ROLE orders_batch {login} PASSWORD '{batch}';\n" + SCHEMA)
        if side == "source":
            ddl += ("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO orders_app;\n"
                "GRANT USAGE,SELECT ON SEQUENCE order_number_seq TO orders_app;\n"
                "GRANT INSERT ON orders TO orders_batch;\n")
        else:
            ddl += ("CREATE DATABASE scratch_orders;\nCREATE DATABASE other_target;\n"
                "REVOKE CONNECT ON DATABASE other_target FROM PUBLIC;\n")
        write(directories["db"], "init.sql", ddl)
        dsn = {"host": db_host, "dbname": database, "user": "orders_app", "password": password,
            "sslmode": "verify-full", "sslrootcert": "/run/sova/database-ca.pem", "connect_timeout": 3}
        if side == "source":
            dsn["hostaddr"] = "10.246.0.10"
        write(directories["app"], "database.dsn", make_conninfo(**dsn))
        write(directories["app"], "database-ca.pem", ca_pem)
        write(directories["app"], "client.token", token)
        write(directories["app"], "config.json", json.dumps({"version": 1,
            "tenant_id": summary["tenant_id"], "application_id": summary["application_id"],
            "run_id": args.run_id, "side": side, "deployment_digest": app_image["manifest_digest"],
            "expires_at": expires.isoformat()}))
        for name, value in ((side + "-admin.password", admin), (side + "-app.password", password),
                            (side + "-batch.password", batch)):
            write(client, name, value)
        summary["environments"][side] = {"database": database, "database_host": db_host,
            "application_host": app_host, "database_certificate_sha256": db_cert,
            "application_certificate_sha256": app_cert, "writer_initially_enabled": side == "source"}

    payload = {kind: {file.name: base64.b64encode(file.read_bytes()).decode()
        for file in sorted((root / ("source-" + kind)).iterdir())} for kind in ("db", "app")}
    encoded = json.dumps({"version": 1, "run_id": args.run_id, "files": payload}).encode()
    if len(encoded) > 65536:
        raise ValueError("Source Secret payload exceeds the service limit")
    write(root, "source-secret.json", encoded)
    write(root, "identity.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"run_id": args.run_id, "prepared": True, "deployed": False,
                      "ca_key_uploaded": False, "source_secret_bytes": len(encoded)}))


if __name__ == "__main__":
    main()
