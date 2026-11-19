"""Measured AWS/VM adapter trial, with separate DBs and a separate real Service.

Does not admit a business plan or issue a signed qualification. The original
orders, privileges and Service selector must be identical before and after.
The trial's new roles/DBs remain closed on failure for explicit investigation.
"""

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from adapter_conformance import apply, readback, SEQUENCES
from postgres_business import prepare
from postgres_world import PostgresWorld
from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls, KubernetesServiceRoute
from sova.adapters.kubernetes.gateway_route import http_side_observer
from sova.domain.contracts import ContractError, EffectStatus
from sova.domain.discovery import KubernetesReadRegistration

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/personal-journey"))
from import_target_images import CONFIG, KUBECTL, VM, guest, identity as verify_vm  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "artifacts"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    inputs = json.loads((args.inputs / "identity.json").read_text())
    links = json.loads(args.connections.read_text())
    if inputs["run_id"] != "b0be270e62" or datetime.now(UTC) >= datetime.fromisoformat(inputs["expires_at"]) - timedelta(minutes=15):
        raise ValueError("Owned trial with at least 15 minutes left required")
    verify_vm()
    namespace = inputs["namespace"]
    ns = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "get", "ns", namespace, "-o", "json"))
    if ns["metadata"]["uid"] != "565b91c4-4f6e-4d06-94c6-9f8803afa6c9":
        raise ValueError("Namespace replaced")
    suffix = uuid4().hex[:10]
    name = "qualification-" + suffix
    names = {"src": "q_source_" + suffix, "tgt": "q_target_" + suffix}
    app, batch = "q_app_" + suffix, "q_batch_" + suffix
    passwords = {app: secrets.token_urlsafe(32), batch: secrets.token_urlsafe(32)}
    names_to_side = {names["src"]: "source", names["tgt"]: "target"}

    def connect(side, database=None, role="postgres", *, autocommit=False):
        entry = inputs["environments"][side]
        password = ((args.inputs / ("client/" + side + "-admin.password")).read_text()
                    if role == "postgres" else passwords[role])
        return psycopg.connect(make_conninfo(host=entry["database_host"], hostaddr="127.0.0.1",
            port=links["ports"][side + "_db"], dbname=database or entry["database"], user=role,
            password=password, sslmode="verify-full", sslrootcert=str(args.inputs / "pki/ca.pem"),
            connect_timeout=5, options="-c statement_timeout=15000 -c lock_timeout=2000"), autocommit=autocommit)

    def snapshot():
        result = {}
        for side in ("source", "target"):
            with connect(side) as conn:
                result[side] = {"counts": list(conn.execute("SELECT (SELECT count(*) FROM orders),"
                    "(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)").fetchone()),
                    "writers": conn.execute("SELECT rolname,rolcanlogin,has_database_privilege(rolname,current_database(),'CONNECT') "
                        "FROM pg_roles WHERE rolname IN ('orders_app','orders_batch') ORDER BY rolname").fetchall()}
        service = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "-n", namespace, "get", "svc", "orders-entry", "-o", "json"))
        result["entry"] = {key: service["metadata"][key] for key in ("uid", "resourceVersion")}
        result["entry"]["selector"] = service["spec"]["selector"]
        return result

    report = {"version": 1, "trial_id": name, "started_at": datetime.now(UTC).isoformat(),
        "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "provider_path": "personal AWS EC2 PostgreSQL to local Lima/RKE2 PostgreSQL",
        "source_file_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            Path(__file__), ROOT / "src/sova/adapters/postgres_offline_copy.py",
            ROOT / "src/sova/adapters/kubernetes/handoff_controls.py", ROOT / "tests/postgres_world.py")},
        "real_business_migration_complete": False, "signed_qualification_issued": False,
        "passed": False, "checks": {}, "steps": []}
    before = snapshot()
    report["original_before"] = before
    ssh = None
    business = None
    created = []
    try:
        for side in ("source", "target"):
            with connect(side, autocommit=True) as conn:
                for role, password in passwords.items():
                    conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
                db = names["src" if side == "source" else "tgt"]
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(db)))
                conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db)))
                if side == "source":
                    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), sql.Identifier(app)))
        business = {"names": names, "app": app, "batch": batch,
            "connect": lambda db: connect(names_to_side[db], db),
            "connect_as": lambda db, role: connect(names_to_side[db], db, role)}
        prepare(names, app, batch, business["connect"], business["connect_as"], schema_on=tuple(names.values()))
        labels = {"sova.run": inputs["run_id"], "sova.qualification": name}
        objects = [
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": namespace, "labels": labels},
             "spec": {"ports": [{"port": 8443, "targetPort": 8443}], "selector": {
                 "sova.run": inputs["run_id"], "sova.role": "edge", "sova.side": "source"}}},
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name, "namespace": namespace, "labels": labels},
             "automountServiceAccountToken": False},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": name, "namespace": namespace, "labels": labels},
             "rules": [{"apiGroups": [""], "resources": ["services"], "resourceNames": [name], "verbs": ["get", "patch"]},
                       {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
                       {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["list"]}]},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": name, "namespace": namespace, "labels": labels},
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
             "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}]}]
        for obj in objects:
            output = subprocess.check_output(["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n", KUBECTL,
                "--kubeconfig", CONFIG, "create", "-f", "-", "-o", "json"], input=json.dumps(obj).encode(), stderr=subprocess.PIPE)
            actual = json.loads(output)
            created.append({"kind": obj["kind"], "name": name, "uid": actual["metadata"]["uid"]})
        service = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "-n", namespace, "get", "svc", name, "-o", "json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        known = args.connections.parent / "known_hosts"
        with (args.artifacts / "entry-ssh.log").open("xb") as log:
            ssh = subprocess.Popen(["ssh", "-F", str(Path.home() / ".lima/sova-rke2-target/ssh.config"),
                "-o", "StrictHostKeyChecking=yes", "-o", "NoHostAuthenticationForLocalhost=no",
                "-o", "UserKnownHostsFile=" + str(known), "-o", "ControlMaster=no", "-o", "ControlPath=none",
                "-o", "ExitOnForwardFailure=yes", "-N", "-L", f"127.0.0.1:{port}:{service['spec']['clusterIP']}:8443",
                "lima-sova-rke2-target"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        token = guest(KUBECTL, "--kubeconfig", CONFIG, "-n", namespace, "create", "token", name, "--duration=10m").decode().strip()
        ca = guest("cat", "/var/lib/rancher/rke2/server/tls/server-ca.crt")
        now = datetime.now(UTC)
        registration = KubernetesReadRegistration(tenant_id=inputs["tenant_id"], application_id=inputs["application_id"],
            environment_id="local-vm-b0be270e62", location="registered-personal-vm", endpoint="https://127.0.0.1:19753",
            approved_addresses=("127.0.0.1",), ca_digest=hashlib.sha256(ca).hexdigest(), namespace=namespace,
            namespace_uid=ns["metadata"]["uid"], revision=1, registered_at=now, valid_until=now+timedelta(minutes=9))
        controls = KubernetesHandoffControls(registration, ca_pem=ca, token_provider=lambda: token)
        observer = http_side_observer(f"https://127.0.0.1:{port}/healthz", header="X-Sova-Side",
            sides={"source_orders": "source", "target_orders": "target"}, ca_file=str(args.inputs / "pki/ca.pem"))
        route = KubernetesServiceRoute(controls, name, {
            names[key]: {"sova.run": inputs["run_id"], "sova.role": "edge", "sova.side": side}
            for key, side in (("src", "source"), ("tgt", "target"))}, observe=observer, samples=3,
            expected_service_uid=service["metadata"]["uid"])
        world = PostgresWorld(business)
        world._route = route
        initial_route = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                initial_route = route.get()
                if initial_route == names["src"]:
                    break
            except (OSError, ContractError) as exc:
                # Startup reads may be retried within a fixed readiness window.
                # Never turn an authorization refusal or invalid spec into PASS.
                if isinstance(exc, ContractError) and not isinstance(exc.__cause__, OSError):
                    raise
            time.sleep(0.2)
        if initial_route != names["src"]:
            raise ValueError("Separate actual trial entry is not source")
        report["trial_service"] = {"uid": service["metadata"]["uid"], "port": port, "original_entry_changed": False}
        for side in ("source", "target"):
            with connect(side) as conn:
                report[side + "_system_identifier"] = conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
        assert report["source_system_identifier"] != report["target_system_identifier"]

        def run_step(adapter, action, direction):
            started = time.monotonic()
            apply(adapter, action)
            status = readback(adapter, action)
            if status != EffectStatus.APPLIED:
                raise ValueError(f"Actual {direction} {action} readback is {status}")
            # A new instance cannot rely on an in-memory success flag.
            fresh = world.adapter() if direction == "forward" else world.reverse_adapter()
            assert readback(fresh, action) == EffectStatus.APPLIED
            report["steps"].append({"direction": direction, "action": action, "status": str(status),
                "elapsed_ms": round((time.monotonic()-started)*1000, 3), "fresh_instance_readback": True})

        for action in SEQUENCES["offline-copy"]:
            run_step(world.adapter(), action, "forward")
            if action == "stage_target":
                assert not world.can_write("target", "app") and not world.can_write("target", "other")
            if action == "fence_source_writes":
                assert not world.can_write("source", "app") and not world.can_write("source", "other")
            if action == "activate_target":
                assert world.can_write("target", "app") and not world.can_write("target", "other")
                with connect("target", names["tgt"]) as conn:
                    conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(app)))
                assert readback(world.adapter(), action) == EffectStatus.UNKNOWN
                report["checks"]["nologin_is_not_activation"] = True
                with connect("target", names["tgt"]) as conn:
                    conn.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(app)))
        assert world.content("source") == world.content("target")
        report["checks"]["forward_content_and_sequence_equal"] = True
        with connect("target", names["tgt"], app) as conn:
            conn.execute("INSERT INTO orders VALUES (51,'after-target','after-target',17.25)")
            conn.execute("INSERT INTO work_items VALUES (51,51,'done')")
            conn.execute("INSERT INTO idempotency_results VALUES ('after-target','after-target','confirmed')")
            conn.execute("SELECT nextval('order_number_seq')")
        for action in ("verify_return_target", "start_sync", "pause_admission", "fence_source_writes", "apply_final_changes", "verify_final_state", "activate_target", "commit_route"):
            run_step(world.reverse_adapter(), action, "reverse")
        assert world.content("source") == world.content("target")
        assert world.can_write("source", "app") and not world.can_write("target", "app")
        with connect("source", names["src"]) as conn:
            assert conn.execute("SELECT count(*) FROM orders WHERE customer='after-target'").fetchone() == (1,)
        report["checks"]["new_target_write_preserved_on_failback"] = True
        report["checks"]["single_writer_after_failback"] = True
        report["checks"]["actual_route_returned_to_source"] = route.get() == names["src"]
        report["passed"] = all(report["checks"].values())
    finally:
        if ssh is not None:
            ssh.terminate()
            ssh.wait(timeout=10)
        report["created_objects"] = created
        # Trial writers are disabled even on failure, without touching original roles.
        for side in ("source", "target"):
            with connect(side) as conn:
                for role in passwords:
                    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
                    if exists:
                        conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role)))
        report["trial_writers_disabled"] = True
        report["original_after"] = snapshot()
        report["checks"]["original_app_data_privileges_and_entry_unchanged"] = before == report["original_after"]
        report["completed_at"] = datetime.now(UTC).isoformat()
        (args.artifacts / "trial.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["checks"]["original_app_data_privileges_and_entry_unchanged"]:
        raise ValueError("Original application changed during qualification trial")
    print(json.dumps({"passed": report["passed"], "steps": len(report["steps"]), "checks": report["checks"], "signed_qualification_issued": False}))


if __name__ == "__main__":
    main()
