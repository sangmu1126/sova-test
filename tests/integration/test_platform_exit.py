"""Platform exit (§20.2, AT-36): the platform is locked out and the customer runs alone.

After a real handoff the platform still holds four credentials for the target
environment: a Kubernetes service-account token, a database role, an
identity-provider administrator and an object-storage account. The customer
revokes each with its own tools; the environment must then refuse every one
of them. With its own credentials only, the customer deploys a new version,
rotates a secret that the new version picks up, stores new data through a new
connection, backs up and restores the database, and resets a user's password.
A pod that keeps answering is not taken as evidence of anything.

``SOVA_TEST_POSTGRES_CONTAINER`` names the database container whose own
``pg_dump``/``pg_restore`` the customer uses for the backup.
"""

import base64
from datetime import UTC, datetime, timedelta
import json
import os
import secrets
import shutil
import subprocess
from uuid import uuid4

from handoff_drive import run_handoff, without_transport_control
import httpx
from keycloak_fixtures import CLIENT
from kind_support import delete_namespace, kind_namespace, kubectl
import psycopg
from psycopg import sql
import pytest

from sova.adapters import platform_lockout as lockout
from sova.adapters.oidc_probes import OidcProbes
from sova.adapters.postgres_handoff_probes import artifact_digest
from sova.adapters.s3_bucket_policy_access import S3BucketPolicyAccess
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.postgres, pytest.mark.kubernetes, pytest.mark.keycloak,
              pytest.mark.s3]
IMAGE = "public.ecr.aws/docker/library/busybox:1.37.0"


def app_deployment(version):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "orders-app"},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "orders"}},
                     "template": {"metadata": {"labels": {"app": "orders"},
                                               "annotations": {"version": version}},
                                  "spec": {"terminationGracePeriodSeconds": 0, "containers": [{
                                      "name": "app", "image": IMAGE,
                                      "imagePullPolicy": "IfNotPresent",
                                      "command": ["sh", "-c", "sleep 3600"],
                                      "env": [{"name": "API_KEY", "valueFrom": {
                                          "secretKeyRef": {"name": "orders-api",
                                                           "key": "api-key"}}}]}]}}}}


@pytest.fixture
def environment(postgres_database, business, keycloak_servers, realm_name, s3_stores):
    """The handed-over target environment and the platform's four credentials."""
    container = os.environ.get("SOVA_TEST_POSTGRES_CONTAINER")
    if not container or not shutil.which("docker") or s3_stores["platform"] is None:
        pytest.skip("SOVA_TEST_POSTGRES_CONTAINER, docker and SOVA_TEST_S3_PLATFORM needed")
    key = secrets.token_urlsafe(24)
    rules = [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "patch"]},
             {"apiGroups": [""], "resources": ["services"], "verbs": ["get"]}]
    objects = [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "orders-api"},
                "data": {"api-key": base64.b64encode(key.encode()).decode()}},
               {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "orders"},
                "spec": {"selector": {"app": "orders"}, "ports": [{"port": 80}]}},
               app_deployment("v1")]
    context, namespace, platform_controls = kind_namespace(rules, objects, prefix="exit")
    kubectl(context, "-n", namespace, "rollout", "status", "deployment/orders-app",
            "--timeout=120s")
    # The platform's database role on the target.
    platform_role = "sova_platform_" + uuid4().hex[:8]
    dsn, tgt = business["dsn"], business["names"]["tgt"]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(platform_role)))
        admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(tgt), sql.Identifier(platform_role)))
    # The platform's administrator at the identity provider, and a business realm.
    kc = keycloak_servers["target"]
    issuer = kc.realm(realm_name)
    kc.user(realm_name, "alice", "Alice-before-1!")
    platform_admin = ("sova-platform-" + uuid4().hex[:6], "Pl-" + secrets.token_urlsafe(16))
    admin_id = kc.user("master", platform_admin[0], platform_admin[1])
    admin_role = kc.call("GET", "/master/roles/admin")
    kc.call("POST", f"/master/users/{admin_id}/role-mappings/realm", [admin_role])
    # The platform's object-storage account on the target bucket.
    root = s3_stores["root"]("target")
    bucket = "orders-exit-" + uuid4().hex[:8]
    root.create_bucket(Bucket=bucket)
    root.put_bucket_policy(Bucket=bucket, Policy=json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Sid": "PlatformAccess", "Effect": "Allow",
            "Principal": {"AWS": [s3_stores["platform_principal"]]}, "Action": ["s3:*"],
            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}]}))
    root.put_object(Bucket=bucket, Key="orders/0001.json", Body=b'{"order": 1}')
    yield {"context": context, "namespace": namespace, "controls": platform_controls,
           "container": container, "key": key, "platform_role": platform_role,
           "kc": kc, "realm": realm_name, "issuer": issuer, "platform_admin": platform_admin,
           "admin_id": admin_id, "root": root, "bucket": bucket, "stores": s3_stores}
    delete_namespace(context, namespace)


def lockouts(env, business):
    tgt = business["names"]["tgt"]
    return {
        "kubernetes": lambda: lockout.kubernetes_token(env["controls"](),
                                                       probe_service="orders"),
        "postgresql": lambda: lockout.postgres_role(lambda: psycopg.connect(
            business["dsn"], dbname=tgt, user=env["platform_role"], connect_timeout=5)),
        "identity-provider": lambda: lockout.oidc_admin(
            httpx.Client(timeout=10), f"{env['kc'].url}/realms/master/protocol/openid-connect"
            "/token", username=env["platform_admin"][0], password=env["platform_admin"][1]),
        "object-storage": lambda: lockout.s3_account(env["stores"]["platform"]("target"),
                                                     env["bucket"]),
    }


def test_the_platform_is_locked_out_and_the_customer_operates_alone(
        postgres_database, business, environment):
    env, names = environment, business["names"]
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), business,
                    adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (
        x["record"].reason, x["record"].held_from, x["observed"].get("uncertain"),
        x["observed"].get("failed"))
    platform = lockouts(env, business)
    before = {name: probe()[0] for name, probe in platform.items()}
    assert before == dict.fromkeys(platform, False)  # the platform can still act

    # The customer revokes everything the platform held, with its own tools.
    kubectl(env["context"], "-n", env["namespace"], "delete", "rolebinding,serviceaccount",
            "sova-handoff")
    with psycopg.connect(business["dsn"], autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(
            sql.Identifier(env["platform_role"])))
    env["kc"].call("DELETE", f"/master/users/{env['admin_id']}")
    S3BucketPolicyAccess(env["root"], env["bucket"]).deny(
        "CustomerRevokedPlatform", (env["stores"]["platform_principal"],))

    rotated = secrets.token_urlsafe(24)

    def deploy_with_rotated_secret():
        """Rotate the API key and roll out a new version that must carry it."""
        encoded = base64.b64encode(rotated.encode()).decode()
        kubectl(env["context"], "-n", env["namespace"], "patch", "secret", "orders-api",
                "--type=merge", "-p", json.dumps({"data": {"api-key": encoded}}))
        kubectl(env["context"], "-n", env["namespace"], "apply", "-f", "-",
                stdin=json.dumps(app_deployment("v2")))
        kubectl(env["context"], "-n", env["namespace"], "rollout", "status",
                "deployment/orders-app", "--timeout=120s")
        pod = kubectl(env["context"], "-n", env["namespace"], "get", "pods", "-l",
                      "app=orders", "--field-selector=status.phase=Running", "-o",
                      "jsonpath={.items[?(@.metadata.annotations.version=='v2')]"
                      ".metadata.name}").split()
        seen = kubectl(env["context"], "-n", env["namespace"], "exec", pod[0], "--",
                       "printenv", "API_KEY").strip() if pod else ""
        return seen == rotated, {"new_version_running": bool(pod),
                                 "new_version_has_rotated_secret": seen == rotated}

    def new_connection_stores_data():
        with business["connect_as"](names["tgt"], business["app"]) as conn:
            conn.execute("INSERT INTO orders VALUES (7001, 'customer-1', 'key-7001', 70)")
            stored = conn.execute("SELECT count(*) FROM orders WHERE id = 7001").fetchone()[0]
        return stored == 1, {"stored": stored}

    def backup_and_restore():
        restored = "restored_" + uuid4().hex[:8]
        dump = subprocess.run(["docker", "exec", env["container"], "pg_dump", "-U", "postgres",
                               "-Fc", names["tgt"]], capture_output=True, timeout=120)
        with psycopg.connect(business["dsn"], autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored)))
        load = subprocess.run(["docker", "exec", "-i", env["container"], "pg_restore", "-U",
                               "postgres", "--no-owner", "-d", restored], input=dump.stdout,
                              capture_output=True, timeout=120)

        def rows(database):
            with psycopg.connect(business["dsn"], dbname=database) as conn:
                return sorted(r[0] for r in conn.execute("SELECT o::text FROM orders o"))
        same = dump.returncode == 0 and load.returncode == 0 and rows(restored) == rows(
            names["tgt"])
        return same, {"backup_bytes": len(dump.stdout), "restored_equal": same}

    def reset_a_users_password():
        user = env["kc"].find(env["realm"], "alice")
        env["kc"].call("PUT", f"/{env['realm']}/users/{user['id']}/reset-password",
                       {"type": "password", "value": "Alice-after-2!", "temporary": False})
        return OidcProbes(client_id=CLIENT).fresh_login(env["issuer"], "alice",
                                                        "Alice-after-2!")

    operations = {"deploy_with_rotated_secret": deploy_with_rotated_secret,
                  "new_connection_stores_data": new_connection_stores_data,
                  "backup_and_restore": backup_and_restore,
                  "reset_a_users_password": reset_a_users_password}
    passed, observation = lockout.platform_exit(platform, operations)
    assert passed, observation
    drill = next(o for o in x["readiness"].obligations(x["revision"], x["plan"])
                 if o.obligation_id == "exit.platform.customer_operates_after_revocation")
    x["prove"](drill.obligation_id, VerificationResult.PASS, obligation=drill,
               artifact_digest=artifact_digest(observation))
    levels = {k: v["status"] for k, v in
              x["readiness"].assess(x["revision"], x["plan"])["levels"].items()}
    assert levels["PLATFORM_EXIT_VERIFIED"] == "PASS", levels
    assert levels["PROVIDER_EXIT_VERIFIED"] == "UNKNOWN"  # not measured in this test
