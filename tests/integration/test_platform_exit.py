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
import os
import secrets
import shutil
from uuid import uuid4

from handoff_drive import run_handoff, without_transport_control
from kind_support import delete_namespace, kind_namespace, kubectl
import platform_exit_support as platform
import pytest

from sova.adapters import platform_lockout as lockout
from sova.adapters.postgres_handoff_probes import artifact_digest
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.postgres, pytest.mark.kubernetes, pytest.mark.keycloak,
              pytest.mark.s3]


@pytest.fixture
def environment(postgres_database, business, keycloak_servers, realm_name, s3_stores):
    """The handed-over target environment and the platform's four credentials."""
    container = os.environ.get("SOVA_TEST_POSTGRES_CONTAINER")
    if not container or not shutil.which("docker") or s3_stores["platform"] is None:
        pytest.skip("SOVA_TEST_POSTGRES_CONTAINER, docker and SOVA_TEST_S3_PLATFORM needed")
    rules = [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "patch"]},
             {"apiGroups": [""], "resources": ["services"], "verbs": ["get"]}]
    objects = [{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "orders-api"},
                "data": {"api-key": base64.b64encode(secrets.token_urlsafe(16).encode()).decode()}},
               {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "orders"},
                "spec": {"selector": {"app": "orders"}, "ports": [{"port": 80}]}},
               platform.app_deployment("v1", secret="orders-api", key="api-key")]
    context, namespace, controls = kind_namespace(rules, objects, prefix="exit")
    kubectl(context, "-n", namespace, "rollout", "status", "deployment/orders-app",
            "--timeout=120s")
    kc = keycloak_servers["target"]
    issuer = kc.realm(realm_name)
    kc.user(realm_name, "alice", "Alice-before-1!")
    root = s3_stores["root"]("target")
    bucket = "orders-exit-" + uuid4().hex[:8]
    root.create_bucket(Bucket=bucket)
    root.put_object(Bucket=bucket, Key="orders/0001.json", Body=b'{"order": 1}')
    granted = platform.grant(admin_dsn=business["dsn"], target_db=business["names"]["tgt"],
                             keycloak=kc, s3_root=root, bucket=bucket,
                             platform_principal=s3_stores["platform_principal"])
    yield {"context": context, "namespace": namespace, "controls": controls,
           "container": container, "kc": kc, "realm": realm_name, "issuer": issuer,
           "root": root, "bucket": bucket, "stores": s3_stores, "granted": granted}
    delete_namespace(context, namespace)


def test_the_platform_is_locked_out_and_the_customer_operates_alone(
        postgres_database, business, environment):
    env, names = environment, business["names"]
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), business,
                    adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (
        x["record"].reason, x["record"].held_from, x["observed"].get("uncertain"),
        x["observed"].get("failed"))
    held = platform.lockouts(
        env["granted"], kubernetes_request=lambda: env["controls"]().service("orders"),
        admin_dsn=business["dsn"], target_db=names["tgt"], keycloak=env["kc"],
        platform_s3=env["stores"]["platform"]("target"), bucket=env["bucket"])
    assert {name: probe()[0] for name, probe in held.items()} == dict.fromkeys(held, False)

    platform.revoke(env["granted"], context=env["context"], namespace=env["namespace"],
                    admin_dsn=business["dsn"], keycloak=env["kc"], s3_root=env["root"],
                    bucket=env["bucket"],
                    platform_principal=env["stores"]["platform_principal"])
    operations = platform.customer_operations(
        context=env["context"], namespace=env["namespace"], secret="orders-api",
        key="api-key", container=env["container"], admin_dsn=business["dsn"],
        target_db=names["tgt"],
        app_connect=lambda: business["connect_as"](names["tgt"], business["app"]),
        keycloak=env["kc"], realm=env["realm"], issuer=env["issuer"], user="alice",
        password="Alice-after-2!")
    passed, observation = lockout.platform_exit(held, operations)
    assert passed, observation
    drill = next(o for o in x["readiness"].obligations(x["revision"], x["plan"])
                 if o.obligation_id == "exit.platform.customer_operates_after_revocation")
    x["prove"](drill.obligation_id, VerificationResult.PASS, obligation=drill,
               artifact_digest=artifact_digest(observation))
    levels = {k: v["status"] for k, v in
              x["readiness"].assess(x["revision"], x["plan"])["levels"].items()}
    assert levels["PLATFORM_EXIT_VERIFIED"] == "PASS", levels
    assert levels["PROVIDER_EXIT_VERIFIED"] == "UNKNOWN"  # not measured in this test
