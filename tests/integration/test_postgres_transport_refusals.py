"""Whether a PostgreSQL server refuses what a weaker client would try.

The test environment's TLS server turns TLS on and keeps the image's default
access rules (``host all all all trust``): it holds its identity - a wrong CA
and a wrong host name are refused - and still takes a startup message in the
clear. ``ssl = on`` is not a refusal of plaintext, and the check says so. A
server whose access rules admit TCP only over TLS (``hostssl``) refuses the
same startup message before asking for any password, and passes.
"""

import base64
import json
import os
from pathlib import Path

import pytest

from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes
from sova.adapters.tls_refusal import TlsEndpoint, postgres_plaintext, transport_refusals

IMAGE = "postgres:17.11"
HBA = "local all all trust\nhostssl all all all scram-sha-256\n"


def test_tls_on_with_the_default_access_rules_still_takes_plaintext():
    root = os.environ.get("SOVA_TEST_POSTGRES_TLS_ROOT")
    if not root:
        pytest.skip("SOVA_TEST_POSTGRES_TLS_ROOT names the test environment's TLS CA")
    endpoint = TlsEndpoint("127.0.0.1", 55433, "localhost", Path(root, "ca.pem").read_bytes(),
                           preamble="postgres")
    passed, observation = transport_refusals(
        endpoint, plaintext=postgres_plaintext("postgres", "sova_test"))
    assert not passed and observation["outcome"] == "accepted"
    assert observation["accepted"] == ["plaintext"]
    checks = observation["checks"]
    assert checks["positive_before"]["outcome"] == "verified"
    assert checks["wrong_ca"]["outcome"] == checks["wrong_host"]["outcome"] == "refused"


@pytest.fixture(scope="module")
def tls_only_postgres():
    from kind_support import delete_namespace, kind_namespace, kubectl
    from mysql_pair_business import tls_material
    from redis_business import _forward

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the TLS-only PostgreSQL")
    context, namespace, _ = kind_namespace([], [], prefix="pg-tls-only")
    process = None
    try:
        material = tls_material(namespace)
        labels = {"app": "pg-tls-only"}
        objects = [
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "pg-tls"},
             "data": {k: base64.b64encode(v).decode() for k, v in material.items()}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "pg-hba"},
             "data": {"pg_hba.conf": HBA}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "pg-tls-only"},
             "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": {
                          "securityContext": {"fsGroup": 999},
                          "containers": [{
                              "name": "postgres", "image": IMAGE,
                              "args": ["-c", "ssl=on",
                                       "-c", "ssl_cert_file=/etc/pg-tls/server-cert.pem",
                                       "-c", "ssl_key_file=/etc/pg-tls/server-key.pem",
                                       "-c", "hba_file=/etc/pg-hba/pg_hba.conf"],
                              "env": [{"name": "POSTGRES_PASSWORD",
                                       "value": "sova-pg-tls-only-test-secret"},
                                      {"name": "POSTGRES_DB", "value": "orders"}],
                              # Over TCP: the image's initialisation server
                              # listens on its socket only, and is not this one.
                              "readinessProbe": {"exec": {"command": [
                                  "pg_isready", "-h", "127.0.0.1", "-U", "postgres",
                                  "-d", "orders"]},
                                  "periodSeconds": 2},
                              "resources": {"limits": {"memory": "256Mi"}},
                              "volumeMounts": [
                                  {"name": "tls", "mountPath": "/etc/pg-tls", "readOnly": True},
                                  {"name": "hba", "mountPath": "/etc/pg-hba",
                                   "readOnly": True}]}],
                          "volumes": [
                              {"name": "tls", "secret": {"secretName": "pg-tls",
                                                         "defaultMode": 0o640}},
                              {"name": "hba", "configMap": {"name": "pg-hba"}}]}}}}]
        kubectl(context, "-n", namespace, "apply", "-f", "-",
                stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))
        kubectl(context, "-n", namespace, "rollout", "status", "deployment/pg-tls-only",
                "--timeout=180s")
        process, port = _forward(context, namespace, "pg-tls-only", 5432)
        yield TlsEndpoint("127.0.0.1", port, "localhost", material["ca.pem"],
                          preamble="postgres")
    finally:
        if process:
            process.terminate()
        delete_namespace(context, namespace)


def test_a_server_admitting_tcp_only_over_tls_refuses_plaintext(tls_only_postgres):
    passed, observation = transport_refusals(
        tls_only_postgres, plaintext=postgres_plaintext("orders_app", "orders"))
    assert passed, observation
    assert observation["checks"]["plaintext"]["reason"] == "server_requires_tls"


def test_the_unit_probes_ask_the_registered_target(tls_only_postgres):
    def unused(*args, **kwargs):
        raise AssertionError("no database connection is needed")

    probes = PostgresHandoffProbes(connect_as=unused, admin_connect=unused,
                                   source_db="orders", target_db="orders",
                                   app_role="orders_app", tables=(("orders", "id"),),
                                   probe_insert="SELECT 1", tls={"target": tls_only_postgres})
    passed, observation = probes.transport_refusals("target")
    assert passed and observation["side"] == "target"
    assert not probes.transport_refusals("source")[0]  # nothing registered there
