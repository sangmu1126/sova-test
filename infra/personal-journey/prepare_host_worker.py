"""Prepare the real native host worker for the owned AWS/VM canary.

Only bounded credential minting and private local files. No plan, approval,
trust bootstrap, admission, route mutation, database role change or data copy.
"""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets

from import_target_images import CONFIG, KUBECTL, guest, identity as verify_vm
from sova.domain.discovery import KubernetesReadRegistration
from sova.qualification.order_schema import SCHEMA, TABLES, PROBE_INSERT

import psycopg
from psycopg.conninfo import make_conninfo


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "entry-connection", "control-directory", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    verify_vm()
    inputs = json.loads((args.inputs / "identity.json").read_text())
    links, entry = json.loads(args.connections.read_text()), json.loads(args.entry_connection.read_text())
    now = datetime.now(UTC)
    expires = datetime.fromisoformat(inputs["expires_at"])
    if inputs["run_id"] != "b0be270e62" or links["run_id"] != inputs["run_id"] or now >= expires:
        raise ValueError("Unexpired owned run required")
    namespace = inputs["namespace"]
    found = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "get", "ns", namespace, "-o", "json"))
    if found["metadata"]["uid"] != "565b91c4-4f6e-4d06-94c6-9f8803afa6c9":
        raise ValueError("Namespace changed")
    args.output.mkdir(mode=0o700, exist_ok=False)
    ca = guest("cat", "/var/lib/rancher/rke2/server/tls/server-ca.crt")
    (args.output / "kubernetes-ca.pem").write_bytes(ca)
    (args.output / "route.token").write_bytes(guest(KUBECTL, "--kubeconfig", CONFIG, "-n", namespace,
                                                  "create", "token", "orders-route-worker", "--duration=10m"))
    registration = KubernetesReadRegistration(
        tenant_id=inputs["tenant_id"], application_id=inputs["application_id"], environment_id="local-vm-b0be270e62",
        location="registered-personal-vm", endpoint="https://127.0.0.1:19753", approved_addresses=("127.0.0.1",),
        ca_digest=hashlib.sha256(ca).hexdigest(), namespace=namespace, namespace_uid=found["metadata"]["uid"],
        revision=1, registered_at=now, valid_until=min(expires, now + timedelta(minutes=10)))
    document = {"schema_version": 1, **asdict(registration), "approved_addresses": list(registration.approved_addresses),
                "registered_at": registration.registered_at.isoformat(), "valid_until": registration.valid_until.isoformat()}
    (args.output / "kubernetes-registration.json").write_text(json.dumps(document))
    databases, passwords = {}, {}
    for name, side in (("source_orders", "source"), ("target_orders", "target"),
                       ("scratch_orders", "target"), ("other_target", "target"),
                       ("scratch_source", "source"), ("other_source", "source")):
        current = inputs["environments"][side]
        dsn = make_conninfo(host=current["database_host"], hostaddr="127.0.0.1", port=links["ports"][side + "_db"],
            dbname=name, user="postgres", password=(args.inputs / ("client/" + side + "-admin.password")).read_text(),
            sslmode="verify-full", sslrootcert=str(args.inputs / "pki/ca.pem"), connect_timeout=5,
            options="-c statement_timeout=10000 -c lock_timeout=1000")
        file = args.output / (name + ".dsn")
        file.write_text(dsn)
        with psycopg.connect(dsn) as conn:
            if conn.execute("SELECT current_database()").fetchone()[0] != name:
                raise ValueError("Database identity differs")
        databases[name] = {"dsn_file": str(file)}
        passwords[name] = {role: str(args.inputs / ("client/" + side + "-" + kind + ".password"))
                           for role, kind in (("orders_app", "app"), ("orders_batch", "batch"))}
    (args.output / "schema.sql").write_text(SCHEMA)
    route = {"kind": "kubernetes_service", "registration_file": str(args.output / "kubernetes-registration.json"),
        "token_file": str(args.output / "route.token"), "ca_file": str(args.output / "kubernetes-ca.pem"),
        "service": "orders-entry", "service_uid": entry["service_uid"], "sides": {
            side + "_orders": {"sova.run": inputs["run_id"], "sova.role": "edge", "sova.side": side}
            for side in ("source", "target")}, "samples": 3,
        "probe": {"url": f"https://127.0.0.1:{entry['port']}/healthz", "header": "X-Sova-Side",
            "values": {"source_orders": "source", "target_orders": "target"}, "ca_file": str(args.inputs / "pki/ca.pem")}}
    common = {"tenant_id": inputs["tenant_id"], "application_id": inputs["application_id"],
        "adapter_id": "pg-personal-offline-v1", "engine": "postgresql", "mode": "offline_copy", "databases": databases,
        "database_role_password_files": passwords, "app_role": "orders_app",
        "writer_roles": ["orders_app", "orders_batch"], "tables": [list(row) for row in TABLES],
        "schema_sql_file": str(args.output / "schema.sql"), "probe_insert": PROBE_INSERT, "route": route}
    from sova.service.directional_catalog import catalog_v2, source_identity

    forward = {**common, "environment_pair": {"source": "aws-ec2-b0be270e62", "target": "local-rke2-b0be270e62"},
        "source_db": "source_orders", "target_db": "target_orders", "scratch_db": "scratch_orders", "other_db": "other_target"}
    reverse = {**common, "environment_pair": {"source": "local-rke2-b0be270e62", "target": "aws-ec2-b0be270e62"},
        "source_db": "target_orders", "target_db": "source_orders", "scratch_db": "scratch_source", "other_db": "other_source"}
    implementation = source_identity()
    (args.output / "implementation-identity.json").write_text(json.dumps(implementation, indent=2))
    catalog = {"version": 2, "implementation_digest": implementation["digest"], "applications": [forward, reverse]}

    # Validate using the same parser the real worker uses, before publishing env.
    _, close = catalog_v2(catalog)
    close()
    (args.output / "execution.json").write_text(json.dumps(catalog))
    (args.output / "evidence.key").write_text(secrets.token_hex(32))
    environment = {"SOVA_DATABASE_URL_FILE": str(args.control_directory / "runtime.dsn"),
        "SOVA_REGISTRY_VERIFIER_VERSION": "registry-attestor-1", "SOVA_EVIDENCE_VERIFIER": "handoff-verifier-1",
        "SOVA_EVIDENCE_ISSUER": "personal-handoff-producer", "SOVA_WITNESS": "file:" + str(args.control_directory / "witness"),
        "SOVA_TENANTS": inputs["tenant_id"], "SOVA_WORKER_IDENTITY": "host",
        "SOVA_EVIDENCE_KEY_FILE": str(args.output / "evidence.key"), "SOVA_EVIDENCE_KEY_ID": "worker-b0be270e62",
        "SOVA_EXECUTION_FILE": str(args.output / "execution.json")}
    (args.output / "environment.json").write_text(json.dumps(environment, indent=2))
    print(json.dumps({"database_connections_verified": len(databases), "route_token_seconds": 600,
        "identity": "actual host process", "catalog_version": 2, "directions": 2,
        "automatic_remote_quiescence": False, "admitted": False}))


if __name__ == "__main__":
    main()
