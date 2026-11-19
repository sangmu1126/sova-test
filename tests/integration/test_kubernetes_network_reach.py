"""Another namespace's reach into the application's data endpoint, on kind.

The application's namespace runs a TCP server behind a Service; another
namespace stands for every other workload of the cluster. Without a policy
the Service answers anyone; a policy admitting only the application's own
clients is enforced by kind's network plugin (kindnetd); a policy admitting
every namespace is no isolation, however it is written.
"""

import json
import os
import time

import pytest
from volume_business import PYTHON, UID

from sova.adapters.kubernetes.network_reach import NetworkReachCheck
from sova.adapters.kubernetes.volume_controls import KubernetesVolumeControls

pytestmark = pytest.mark.kubernetes
RULES = [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "create", "delete"]},
         {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}]
SERVER = ("import socket\ns = socket.create_server(('', 8080))\n"
          "while True:\n    s.accept()[0].close()\n")


def server_objects():
    labels = {"app": "orders-db"}
    return [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "orders-db"},
             "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": {
                          "automountServiceAccountToken": False,
                          "securityContext": {"runAsNonRoot": True, "runAsUser": UID,
                                              "runAsGroup": UID,
                                              "seccompProfile": {"type": "RuntimeDefault"}},
                          "containers": [{
                              "name": "db", "image": PYTHON,
                              "command": ["python3", "-I", "-c", SERVER],
                              "ports": [{"containerPort": 8080}],
                              "readinessProbe": {"tcpSocket": {"port": 8080},
                                                 "periodSeconds": 1},
                              "securityContext": {"allowPrivilegeEscalation": False,
                                                  "readOnlyRootFilesystem": True,
                                                  "capabilities": {"drop": ["ALL"]}}}]}}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "orders-db"},
             "spec": {"selector": labels, "ports": [{"port": 5432, "targetPort": 8080}]}}]


def policy(ingress_from):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "orders-db-clients"},
            "spec": {"podSelector": {"matchLabels": {"app": "orders-db"}},
                     "policyTypes": ["Ingress"],
                     "ingress": [{"from": ingress_from, "ports": [{"port": 8080}]}]}}


@pytest.fixture(scope="module")
def namespaces():
    from kind_support import delete_namespace, kind_namespace, kubectl

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the network reach tests")
    context, inside, inside_controls = kind_namespace(
        RULES, server_objects(), prefix="reach-app", controls_class=KubernetesVolumeControls)
    _, outside, outside_controls = kind_namespace(
        RULES, [], prefix="reach-other", controls_class=KubernetesVolumeControls)
    try:
        kubectl(context, "-n", inside, "rollout", "status", "deployment/orders-db",
                "--timeout=180s")

        def apply(document):
            kubectl(context, "-n", inside, "apply", "-f", "-", stdin=json.dumps(document))
            time.sleep(5)  # the network plugin programs a new policy within seconds

        def remove():
            kubectl(context, "-n", inside, "delete", "networkpolicy", "--all")
        yield {"inside": inside, "outside": outside, "apply": apply, "remove": remove,
               "check": NetworkReachCheck(
                   inside=inside_controls(), outside=[(outside, outside_controls())],
                   host=f"orders-db.{inside}.svc", port=5432,
                   client_labels={"role": "orders-client"}, image=PYTHON, run_as=(UID, UID),
                   connect_seconds=4)}
    finally:
        delete_namespace(context, inside)
        delete_namespace(context, outside)


def test_without_a_policy_any_namespace_reaches_the_endpoint(namespaces):
    namespaces["remove"]()
    passed, observation = namespaces["check"].measure()
    assert not passed and observation["outcome"] == "reached"
    assert observation["outside"] == {namespaces["outside"]: "connected"}


def test_a_policy_admitting_only_the_applications_clients_is_enforced(namespaces):
    namespaces["apply"](policy([{"podSelector": {"matchLabels": {"role": "orders-client"}}}]))
    try:
        passed, observation = namespaces["check"].measure()
    finally:
        namespaces["remove"]()
    assert passed, observation
    assert observation["positive"] == {"before": "connected", "after": "connected"}
    assert observation["outside"][namespaces["outside"]] in ("refused", "timed_out")


def test_a_policy_admitting_every_namespace_is_no_isolation(namespaces):
    namespaces["apply"](policy([{"namespaceSelector": {}}]))
    try:
        passed, observation = namespaces["check"].measure()
    finally:
        namespaces["remove"]()
    assert not passed and observation["outcome"] == "reached"
