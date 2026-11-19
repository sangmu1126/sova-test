"""Another workload's reach into an application's Secrets, on the kind cluster.

The namespace holds the application's Secret and the service accounts of
other workloads with the grants a real cluster accumulates: none, a direct
read of the Secret, a right to create Deployments (which mount any Secret of
the namespace), and a read granted to every service account of another
namespace through its group. Each other principal is measured with its own
token and with the API server's own decision.
"""

import base64
import json
import os
from uuid import uuid4

import pytest

from sova.adapters.kubernetes_secret_probes import (
    KubernetesSecretProbes,
    SecretAccessControls,
    ServiceAccount,
)

pytestmark = pytest.mark.kubernetes


def role(name, rules, subjects):
    return [{"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
             "metadata": {"name": name}, "rules": rules},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
             "metadata": {"name": name},
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
             "subjects": subjects}]


@pytest.fixture(scope="module")
def cluster():
    from kind_support import delete_namespace, kind_namespace, kubectl

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the Secret access tests")
    elsewhere = "secret-others-" + uuid4().hex[:8]
    sa = [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": n}}
          for n in ("orders-app", "billing", "ops", "deployer")]
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "orders-db"},
              "data": {"password": base64.b64encode(b"sova-secret-probe-test-value").decode()}}
    objects = sa + [secret] + role(
        "ops-reads-orders-db",
        [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["orders-db"],
          "verbs": ["get"]}],
        [{"kind": "ServiceAccount", "name": "ops"}]) + role(
        "deployer-creates-deployments",
        [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["create"]}],
        [{"kind": "ServiceAccount", "name": "deployer"}])
    context, namespace, controls = kind_namespace(
        [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}], objects,
        prefix="secret-access")
    reviewer_role = f"sova-access-review-{namespace}"
    try:
        kubectl(context, "create", "namespace", elsewhere)
        kubectl(context, "-n", elsewhere, "create", "serviceaccount", "reports")
        kubectl(context, "apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "List", "items": [
                {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                 "metadata": {"name": reviewer_role},
                 "rules": [{"apiGroups": ["authorization.k8s.io"],
                            "resources": ["subjectaccessreviews"], "verbs": ["create"]}]},
                {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                 "metadata": {"name": reviewer_role},
                 "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                             "name": reviewer_role},
                 "subjects": [{"kind": "ServiceAccount", "name": "sova-handoff",
                               "namespace": namespace}]}]}))
        ca = base64.b64decode(kubectl(
            context, "config", "view", "--raw", "--minify", "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority-data}").strip())
        registration = controls().registration
        tokens = {}

        def token(account):
            if account not in tokens:
                tokens[account] = kubectl(context, "-n", account.namespace, "create", "token",
                                          account.name, "--duration=600s").strip()
            return tokens[account]

        platform = ServiceAccount(namespace, "sova-handoff")

        def as_account(account):
            return SecretAccessControls(registration, ca_pem=ca,
                                        token_provider=lambda: token(account))

        yield {"context": context, "namespace": namespace, "elsewhere": elsewhere,
               "reader": as_account(platform), "as_account": as_account}
    finally:
        for kind in ("clusterrolebinding", "clusterrole"):
            try:
                kubectl(context, "delete", kind, reviewer_role, "--wait=false")
            except AssertionError:
                pass
        try:
            kubectl(context, "delete", "namespace", elsewhere, "--wait=false")
        except AssertionError:
            pass
        delete_namespace(context, namespace)


def probes(cluster, *others, secrets=("orders-db",)):
    return KubernetesSecretProbes(reader=cluster["reader"], reviewer=cluster["reader"],
                                  as_account=cluster["as_account"], secrets=secrets,
                                  others=others)


def test_other_workloads_without_grants_are_refused_every_way(cluster):
    passed, observation = probes(
        cluster, ServiceAccount(cluster["namespace"], "billing"),
        ServiceAccount(cluster["elsewhere"], "reports")).cross_workload_secret_denied()
    assert passed, observation
    billing = observation["principals"][
        f"system:serviceaccount:{cluster['namespace']}:billing"]
    assert billing["attempt"] == {"get:orders-db": "refused", "list:secrets": "refused",
                                  "create_pod_mounting_secret": "refused"}
    assert billing["review_allowed"] == []
    assert "sova-secret-probe-test-value" not in json.dumps(observation)


def test_a_direct_read_grant_fails(cluster):
    passed, observation = probes(
        cluster, ServiceAccount(cluster["namespace"], "ops")).cross_workload_secret_denied()
    assert not passed and observation["outcome"] == "allowed"
    ops = observation["principals"][f"system:serviceaccount:{cluster['namespace']}:ops"]
    assert ops["attempt"]["get:orders-db"] == "allowed"
    assert ops["review_allowed"] == ["get:orders-db"]


def test_a_right_to_create_deployments_is_a_right_to_the_secrets(cluster):
    """No Secret grant at all, and the Pod dry run is refused; the API server
    still says the account may create Deployments, which mount any Secret of
    the namespace."""
    passed, observation = probes(
        cluster, ServiceAccount(cluster["namespace"], "deployer")).cross_workload_secret_denied()
    assert not passed and observation["outcome"] == "allowed"
    deployer = observation["principals"][
        f"system:serviceaccount:{cluster['namespace']}:deployer"]
    assert set(deployer["attempt"].values()) == {"refused"}
    assert deployer["review_allowed"] == ["create:deployments"]


def test_a_grant_to_a_group_reaches_its_service_accounts(cluster):
    from kind_support import kubectl

    reports = ServiceAccount(cluster["elsewhere"], "reports")
    kubectl(cluster["context"], "-n", cluster["namespace"], "apply", "-f", "-",
            stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": role(
                "elsewhere-reads-secrets",
                [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}],
                [{"kind": "Group", "apiGroup": "rbac.authorization.k8s.io",
                  "name": f"system:serviceaccounts:{cluster['elsewhere']}"}])}))
    try:
        passed, observation = probes(cluster, reports).cross_workload_secret_denied()
    finally:
        kubectl(cluster["context"], "-n", cluster["namespace"], "delete", "role,rolebinding",
                "elsewhere-reads-secrets")
    assert not passed and observation["outcome"] == "allowed"
    found = observation["principals"][reports.user]
    assert found["attempt"]["get:orders-db"] == "allowed"
    assert "get:orders-db" in found["review_allowed"]


def test_a_secret_that_is_not_there_leaves_the_check_undecided(cluster):
    passed, observation = probes(cluster, ServiceAccount(cluster["namespace"], "billing"),
                                 secrets=("orders-db", "not-created")
                                 ).cross_workload_secret_denied()
    assert not passed and observation["outcome"] == "undecided"
    assert observation["positive"]["before"] == ["allowed", "undecided:404"]
