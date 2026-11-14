"""A namespace on a disposable kind cluster, prepared as its owner would.

The code under test gets only a namespace-scoped service-account token with
the rules a test grants, through the pinned transport registered for the
loopback API server. ``SOVA_TEST_KUBE_CONTEXT`` must name a kind context, and
``SOVA_TEST_KUBECONFIG`` may name the kubeconfig file it lives in.
"""

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import shutil
import subprocess
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls
from sova.domain.discovery import KubernetesReadRegistration


def kubectl(context, *args, stdin=None):
    """kubectl against ``context``; ``SOVA_TEST_KUBECONFIG`` names a kubeconfig
    file of its own (the test environment never edits ~/.kube/config)."""
    config = os.environ.get("SOVA_TEST_KUBECONFIG")
    prefix = ["kubectl", *(["--kubeconfig", config] if config else []), "--context", context]
    result = subprocess.run([*prefix, *args], input=stdin,
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise AssertionError(f"kubectl {args[0]} failed: {result.stderr[-400:]}")
    return result.stdout


def kind_namespace(rules, objects, *, prefix="handoff"):
    """Create a namespace with ``objects`` and a token limited to ``rules``.
    Returns (context, namespace, controls factory); the caller deletes the
    namespace with ``delete_namespace``."""
    context = os.environ.get("SOVA_TEST_KUBE_CONTEXT")
    if not context or not shutil.which("kubectl"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT and kubectl are required for kind tests")
    if not context.startswith("kind-"):
        raise pytest.UsageError("Kubernetes handoff tests run only against kind clusters")
    server = kubectl(context, "config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}").strip()
    if urlsplit(server).hostname != "127.0.0.1":
        raise pytest.UsageError("Kubernetes handoff tests run only against loopback clusters")
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
            "tenant-orders", "orders", "kind-local", "local", server, ("127.0.0.1",),
            hashlib.sha256(ca).hexdigest(), namespace_name, namespace_uid, 1,
            now - timedelta(minutes=1), now + timedelta(hours=1))
        return KubernetesHandoffControls(registration, ca_pem=ca, token_provider=lambda: token)

    return context, namespace, controls


def delete_namespace(context, namespace):
    kubectl(context, "delete", "namespace", namespace, "--wait=false")
