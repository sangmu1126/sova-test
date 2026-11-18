"""A namespace on a disposable cluster, prepared as its owner would.

The code under test gets only a namespace-scoped service-account token with
the rules a test grants, through the pinned transport registered for the API
server. ``SOVA_TEST_KUBE_CONTEXT`` names the cluster's context and
``SOVA_TEST_KUBECONFIG`` the kubeconfig file it lives in.

Two kinds of cluster are accepted, nothing else: a kind cluster whose API
server is on loopback, or this cloud run's own managed cluster (EKS or GKE,
created by ``cloud-env.sh kubernetes``): ``SOVA_TEST_KUBE_MANAGED_RUN`` names
the run and the context must be ``sova-test-k8s-<run>-eks`` or ``-gke``. The
registration then pins the API server's resolved public addresses.
"""

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import shutil
import socket
import subprocess
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls
from sova.domain.discovery import KubernetesReadRegistration


def _config(context):
    """The kubeconfig file of ``context``: the second test cluster has its own
    (``SOVA_TEST_KUBECONFIG_B``)."""
    if context and context == os.environ.get("SOVA_TEST_KUBE_CONTEXT_B"):
        return os.environ.get("SOVA_TEST_KUBECONFIG_B")
    return os.environ.get("SOVA_TEST_KUBECONFIG")


def storage_class(context=None):
    """The class a test's volumes come from: kind's ``standard``, or the cloud
    run's own class on its managed clusters (every disk it makes carries the
    run, so ``cloud-env.sh down`` finds what a deleted cluster leaves)."""
    context = context or os.environ.get("SOVA_TEST_KUBE_CONTEXT") or ""
    run = os.environ.get("SOVA_TEST_KUBE_MANAGED_RUN")
    if run and context in (f"sova-test-k8s-{run}-eks", f"sova-test-k8s-{run}-gke"):
        return os.environ.get("SOVA_TEST_KUBE_MANAGED_STORAGE_CLASS") or f"sova-test-{run}"
    return "standard"


def kubectl(context, *args, stdin=None):
    """kubectl against ``context``; ``SOVA_TEST_KUBECONFIG`` names a kubeconfig
    file of its own (the test environment never edits ~/.kube/config)."""
    config = _config(context)
    prefix = ["kubectl", *(["--kubeconfig", config] if config else []), "--context", context]
    result = subprocess.run([*prefix, *args], input=stdin,
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise AssertionError(f"kubectl {args[0]} failed: {result.stderr[-400:]}")
    return result.stdout


def cluster_identity(context, server):
    """(environment, location, pinned addresses) of an accepted cluster."""
    host = urlsplit(server).hostname
    run = os.environ.get("SOVA_TEST_KUBE_MANAGED_RUN")
    if context.startswith("kind-"):
        if host != "127.0.0.1":
            raise pytest.UsageError("kind clusters are accepted only on loopback")
        return "kind-local", "local", ("127.0.0.1",)
    if not run or context not in (f"sova-test-k8s-{run}-eks", f"sova-test-k8s-{run}-gke"):
        raise pytest.UsageError("Only kind or this run's own managed clusters are accepted")
    addresses = tuple(sorted({info[4][0] for info in socket.getaddrinfo(
        host, 443, socket.AF_INET, socket.SOCK_STREAM)}))[:8]
    provider = context.rsplit("-", 1)[1]
    return f"managed-{provider}", os.environ.get(f"SOVA_TEST_{'AWS' if provider == 'eks' else 'GCP'}_REGION", "cloud"), addresses


def kind_namespace(rules, objects, *, prefix="handoff", controls_class=KubernetesHandoffControls,
                   context=None):
    """Create a namespace with ``objects`` and a token limited to ``rules``, in
    ``context`` (default: ``SOVA_TEST_KUBE_CONTEXT``). Returns (context,
    namespace, controls factory); the caller deletes the namespace with
    ``delete_namespace``."""
    context = context or os.environ.get("SOVA_TEST_KUBE_CONTEXT")
    if not context or not shutil.which("kubectl"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT and kubectl are required for kind tests")
    server = kubectl(context, "config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}").strip()
    environment, location, addresses = cluster_identity(context, server)
    namespace = f"{prefix}-{uuid4().hex[:8]}"
    kubectl(context, "create", "namespace", namespace)
    access = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "sova-handoff"}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {"name": "sova-handoff"}, "rules": rules},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": "sova-handoff"},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                     "name": "sova-handoff"},
         "subjects": [{"kind": "ServiceAccount", "name": "sova-handoff"}]}]
    kubectl(context, "-n", namespace, "apply", "-f", "-",
            stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": access + objects}))
    uid = kubectl(context, "get", "namespace", namespace, "-o",
                  "jsonpath={.metadata.uid}").strip()
    ca = base64.b64decode(kubectl(
        context, "config", "view", "--raw", "--minify", "-o",
        "jsonpath={.clusters[0].cluster.certificate-authority-data}").strip())
    token = kubectl(context, "-n", namespace, "create", "token", "sova-handoff",
                    "--duration=3600s").strip()
    now = datetime.now(UTC)

    def controls(namespace_name=namespace, namespace_uid=uid):
        registration = KubernetesReadRegistration(
            "tenant-orders", "orders", environment, location, server, addresses,
            hashlib.sha256(ca).hexdigest(), namespace_name, namespace_uid, 1,
            now - timedelta(minutes=1), now + timedelta(hours=1))
        return controls_class(registration, ca_pem=ca, token_provider=lambda: token)

    return context, namespace, controls


def delete_namespace(context, namespace):
    kubectl(context, "delete", "namespace", namespace, "--wait=false")
