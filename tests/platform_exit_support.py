"""The platform's credentials in a handed-over environment, and the customer's tools.

Shared by the local platform-exit test and the complete AWS exit. ``grant``
gives the platform what it typically holds after a handoff (a database role,
an identity-provider administrator, an object-storage account; the Kubernetes
token comes from the namespace it was registered for), ``lockouts`` measures
each of them, ``revoke`` is what the customer does with its own tools, and
``customer_operations`` is what the customer must then manage alone.
"""

import base64
import json
import secrets
import subprocess
from uuid import uuid4

import httpx
from keycloak_fixtures import CLIENT
from kind_support import kubectl
import psycopg
from psycopg import sql

from sova.adapters import platform_lockout as lockout
from sova.adapters.oidc_probes import OidcProbes
from sova.adapters.s3_bucket_policy_access import S3BucketPolicyAccess

IMAGE = "public.ecr.aws/docker/library/busybox:1.37.0"


def app_deployment(version, *, secret, key):
    """An application that reads its API key from ``secret`` at start."""
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "orders-app"},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "orders"}},
                     "template": {"metadata": {"labels": {"app": "orders"},
                                               "annotations": {"version": version}},
                                  "spec": {"terminationGracePeriodSeconds": 0, "containers": [{
                                      "name": "app", "image": IMAGE,
                                      "imagePullPolicy": "IfNotPresent",
                                      "command": ["sh", "-c", "sleep 3600"],
                                      "env": [{"name": "API_KEY", "valueFrom": {
                                          "secretKeyRef": {"name": secret, "key": key}}}]}]}}}}


def grant(*, admin_dsn, target_db, keycloak, s3_root, bucket, platform_principal):
    """Give the platform its database role, IdP administrator and bucket access."""
    role = "sova_platform_" + uuid4().hex[:8]
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(target_db), sql.Identifier(role)))
    admin_user = ("sova-platform-" + uuid4().hex[:6], "Pl-" + secrets.token_urlsafe(16))
    admin_id = keycloak.user("master", admin_user[0], admin_user[1])
    keycloak.call("POST", f"/master/users/{admin_id}/role-mappings/realm",
                  [keycloak.call("GET", "/master/roles/admin")])
    access = S3BucketPolicyAccess(s3_root, bucket)
    policy = access._existing()
    policy["Statement"].append({
        "Sid": "PlatformAccess", "Effect": "Allow", "Principal": {"AWS": [platform_principal]},
        "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]})
    access._store(policy)
    return {"role": role, "admin_user": admin_user, "admin_id": admin_id}


def lockouts(granted, *, kubernetes_request, admin_dsn, target_db, keycloak, platform_s3,
             bucket):
    """``kubernetes_request()`` makes one request the platform's token was allowed."""
    return {
        "kubernetes": lambda: lockout.kubernetes_token(kubernetes_request),
        "postgresql": lambda: lockout.postgres_role(lambda: psycopg.connect(
            admin_dsn, dbname=target_db, user=granted["role"], connect_timeout=5)),
        "identity-provider": lambda: lockout.oidc_admin(
            httpx.Client(timeout=10), f"{keycloak.url}/realms/master/protocol/openid-connect"
            "/token", username=granted["admin_user"][0], password=granted["admin_user"][1]),
        "object-storage": lambda: lockout.s3_account(platform_s3, bucket),
    }


def revoke(granted, *, context, namespace, admin_dsn, keycloak, s3_root, bucket,
           platform_principal):
    """The customer revokes everything the platform held, with its own tools."""
    kubectl(context, "-n", namespace, "delete", "rolebinding,serviceaccount", "sova-handoff")
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(granted["role"])))
    keycloak.call("DELETE", f"/master/users/{granted['admin_id']}")
    S3BucketPolicyAccess(s3_root, bucket).deny("CustomerRevokedPlatform", (platform_principal,))


def customer_operations(*, context, namespace, secret, key, container, admin_dsn, target_db,
                        app_connect, keycloak, realm, issuer, user, password):
    rotated = secrets.token_urlsafe(24)

    def deploy_with_rotated_secret():
        encoded = base64.b64encode(rotated.encode()).decode()
        kubectl(context, "-n", namespace, "patch", "secret", secret, "--type=merge", "-p",
                json.dumps({"data": {key: encoded}}))
        kubectl(context, "-n", namespace, "apply", "-f", "-",
                stdin=json.dumps(app_deployment("v2", secret=secret, key=key)))
        kubectl(context, "-n", namespace, "rollout", "status", "deployment/orders-app",
                "--timeout=120s")
        pod = kubectl(context, "-n", namespace, "get", "pods", "-l", "app=orders",
                      "--field-selector=status.phase=Running", "-o",
                      "jsonpath={.items[?(@.metadata.annotations.version=='v2')]"
                      ".metadata.name}").split()
        seen = kubectl(context, "-n", namespace, "exec", pod[0], "--", "printenv",
                       "API_KEY").strip() if pod else ""
        return seen == rotated, {"new_version_running": bool(pod),
                                 "new_version_has_rotated_secret": seen == rotated}

    def new_connection_stores_data():
        with app_connect() as conn:
            conn.execute("INSERT INTO orders VALUES (7001, 'customer-1', 'key-7001', 70)")
            stored = conn.execute("SELECT count(*) FROM orders WHERE id = 7001").fetchone()[0]
        return stored == 1, {"stored": stored}

    def backup_and_restore():
        restored = "restored_" + uuid4().hex[:8]
        dump = subprocess.run(["docker", "exec", container, "pg_dump", "-U", "postgres", "-Fc",
                               target_db], capture_output=True, timeout=120)
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored)))
        load = subprocess.run(["docker", "exec", "-i", container, "pg_restore", "-U",
                               "postgres", "--no-owner", "-d", restored], input=dump.stdout,
                              capture_output=True, timeout=120)

        def rows(database):
            with psycopg.connect(admin_dsn, dbname=database) as conn:
                return sorted(r[0] for r in conn.execute("SELECT o::text FROM orders o"))
        same = dump.returncode == 0 and load.returncode == 0 and rows(restored) == rows(
            target_db)
        return same, {"backup_bytes": len(dump.stdout), "restored_equal": same}

    def reset_a_users_password():
        found = keycloak.find(realm, user)
        keycloak.call("PUT", f"/{realm}/users/{found['id']}/reset-password",
                      {"type": "password", "value": password, "temporary": False})
        return OidcProbes(client_id=CLIENT).fresh_login(issuer, user, password)

    return {"deploy_with_rotated_secret": deploy_with_rotated_secret,
            "new_connection_stores_data": new_connection_stores_data,
            "backup_and_restore": backup_and_restore,
            "reset_a_users_password": reset_a_users_password}
