"""Traffic route and controller pause against a real, disposable kind cluster.

The environment is prepared with kubectl as the cluster owner would; the code
under test only holds a namespace-scoped service-account token with the
least privileges it needs (read services, endpoint slices, pods, CronJobs and
Jobs; patch services and CronJobs). Pods run the pause image already on the
kind node, so nothing is pulled. ``SOVA_TEST_KUBE_CONTEXT`` must name a kind
context whose API server is on loopback.
"""

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import shutil
import subprocess
from threading import Timer
import time
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from sova.adapters.kubernetes.handoff_controls import (
    KubernetesHandoffControls,
    KubernetesServiceRoute,
    controller_paused,
    pause_cronjob,
)
from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope
from sova.domain.discovery import KubernetesReadRegistration

pytestmark = pytest.mark.kubernetes
PAUSE = "registry.k8s.io/pause:3.10"
SIDES = {"orders_src": {"app": "orders", "side": "source"},
         "orders_tgt": {"app": "orders", "side": "target"}}


def kubectl(context, *args, stdin=None):
    result = subprocess.run(["kubectl", "--context", context, *args], input=stdin,
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise AssertionError(f"kubectl {args[0]} failed: {result.stderr[-400:]}")
    return result.stdout


def deployment(name, side):
    labels = {"app": "orders", "side": side}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": 2, "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels}, "spec": {
                         "terminationGracePeriodSeconds": 0,
                         "containers": [{"name": "app", "image": PAUSE,
                                         "imagePullPolicy": "IfNotPresent"}]}}}}


@pytest.fixture(scope="module")
def cluster():
    context = os.environ.get("SOVA_TEST_KUBE_CONTEXT")
    if not context or not shutil.which("kubectl"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT and kubectl are required for kind tests")
    if not context.startswith("kind-"):
        raise pytest.UsageError("Kubernetes handoff tests run only against kind clusters")
    server = kubectl(context, "config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}").strip()
    if urlsplit(server).hostname != "127.0.0.1":
        raise pytest.UsageError("Kubernetes handoff tests run only against loopback clusters")
    namespace = "handoff-" + uuid4().hex[:8]
    kubectl(context, "create", "namespace", namespace)
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
            "metadata": {"name": "sova-handoff"}, "rules": [
                {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "patch"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
                {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"],
                 "verbs": ["list"]},
                {"apiGroups": ["batch"], "resources": ["cronjobs"], "verbs": ["get", "patch"]},
                {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list"]}]}
    objects = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "sova-handoff"}},
        role,
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": "sova-handoff"},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                     "name": "sova-handoff"},
         "subjects": [{"kind": "ServiceAccount", "name": "sova-handoff"}]},
        deployment("orders-source", "source"), deployment("orders-target", "target"),
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "orders"},
         "spec": {"selector": SIDES["orders_src"], "ports": [{"port": 80}]}},
        {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "nightly-batch"},
         "spec": {"schedule": "0 3 1 1 *", "jobTemplate": {"spec": {"template": {"spec": {
             "restartPolicy": "Never", "terminationGracePeriodSeconds": 0,
             "containers": [{"name": "batch", "image": PAUSE,
                             "imagePullPolicy": "IfNotPresent"}]}}}}}},
    ]
    kubectl(context, "-n", namespace, "apply", "-f", "-",
            stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))
    for name in ("orders-source", "orders-target"):
        kubectl(context, "-n", namespace, "rollout", "status", f"deployment/{name}",
                "--timeout=120s")
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

    yield {"context": context, "namespace": namespace, "controls": controls}
    kubectl(context, "delete", "namespace", namespace, "--wait=false")


def run(cluster, *args, stdin=None):
    return kubectl(cluster["context"], "-n", cluster["namespace"], *args, stdin=stdin)


@pytest.fixture
def route(cluster):
    run(cluster, "patch", "service", "orders", "--type=merge", "-p",
        json.dumps({"spec": {"selector": SIDES["orders_src"]}}))
    route = KubernetesServiceRoute(cluster["controls"](), "orders", SIDES)
    deadline = time.monotonic() + 30
    while route.get() != "orders_src" and time.monotonic() < deadline:
        time.sleep(0.25)
    return route


def command(adapter_id, action):
    return EffectCommand(
        ResourceScope("tenant-orders", "orders", "edge", "unit-orders"), "op-route", action,
        adapter_id, action, f"op-route:{action}", "e" * 64, "revision-1", 1,
        datetime.now(UTC) + timedelta(minutes=10), "{}")


def test_traffic_moves_only_when_every_endpoint_is_a_target_pod(route):
    assert route.get() == "orders_src"
    route.set("orders_tgt")
    observed = route.observe()
    assert observed["route"] == "orders_tgt" and observed["ready_endpoints"] == 2, observed
    route.set("orders_tgt")  # idempotent
    assert route.get() == "orders_tgt"


def test_switching_waits_for_target_pods_and_never_reports_a_route_without_them(
        cluster, route):
    run(cluster, "scale", "deployment/orders-target", "--replicas=0")
    run(cluster, "wait", "--for=delete", "pod", "-l", "side=target", "--timeout=60s")
    slow = KubernetesServiceRoute(cluster["controls"](), "orders", SIDES, converge_seconds=0)
    slow.set("orders_tgt")  # selector moved, but no target pod exists yet
    assert slow.observe()["route"] is None  # traffic reaches no one: not the target
    restore = Timer(1.0, run, (cluster, "scale", "deployment/orders-target", "--replicas=2"))
    restore.start()
    try:
        route.converge_seconds = 90
        route.set("orders_tgt")  # returns once the target pods are behind the Service
        assert route.get() == "orders_tgt"
    finally:
        restore.join()
        run(cluster, "rollout", "status", "deployment/orders-target", "--timeout=120s")


def test_commit_route_of_a_data_adapter_switches_real_traffic(cluster, route):
    """The PostgreSQL adapter's route step, with the Service as its route store:
    the step never touches a database, only the route."""
    adapter = PostgresOfflineCopyAdapter(
        "postgres-offline-copy", connect=lambda database: None, source_db="orders_src",
        target_db="orders_tgt", app_role="orders_app", tables=(("orders", "id"),),
        schema_sql="", route=route)
    cmd = command(adapter.adapter_id, "commit_route")
    adapter.validate(cmd)
    adapter.apply(cmd)
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.APPLIED
    # Someone points the Service back outside the plan.
    run(cluster, "patch", "service", "orders", "--type=merge", "-p",
        json.dumps({"spec": {"selector": SIDES["orders_src"]}}))
    assert adapter.readback(cmd, datetime.now(UTC)).status == EffectStatus.UNKNOWN


def test_a_hand_made_endpoint_behind_the_service_keeps_the_route_undecided(cluster, route):
    route.set("orders_tgt")
    stray = {"apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
             "metadata": {"name": "orders-stray",
                          "labels": {"kubernetes.io/service-name": "orders",
                                     "endpointslice.kubernetes.io/managed-by": "someone"}},
             "addressType": "IPv4", "ports": [{"port": 80}],
             "endpoints": [{"addresses": ["10.250.0.9"], "conditions": {"ready": True}}]}
    run(cluster, "apply", "-f", "-", stdin=json.dumps(stray))
    try:
        observed = route.observe()
        assert observed["route"] is None and observed["foreign_endpoints"] == ["None"], observed
    finally:
        run(cluster, "delete", "endpointslice", "orders-stray")
    assert route.get() == "orders_tgt"


def test_a_change_made_in_between_is_refused_not_overwritten(cluster, route):
    controls = cluster["controls"]()
    stale = controls.service("orders")["metadata"]["resourceVersion"]
    run(cluster, "label", "service", "orders", f"touched={uuid4().hex[:6]}", "--overwrite")
    with pytest.raises(KubernetesRequestRefused):
        controls.set_service_selector("orders", stale, SIDES["orders_tgt"])
    assert route.get() == "orders_src"


def test_a_suspended_batch_still_running_a_job_is_not_paused(cluster):
    controls = cluster["controls"]()
    passed, observation = controller_paused(controls, "nightly-batch")
    assert not passed and observation["suspended"] is False
    run(cluster, "create", "job", "nightly-manual", "--from=cronjob/nightly-batch")
    pause_cronjob(controls, "nightly-batch")
    pause_cronjob(controls, "nightly-batch")  # idempotent
    passed, observation = controller_paused(controls, "nightly-batch")
    assert not passed and observation == {
        "probe": "controller_paused", "controller": "nightly-batch", "suspended": True,
        "running_jobs": ["nightly-manual"]}
    run(cluster, "delete", "job", "nightly-manual", "--wait=true")
    assert controller_paused(controls, "nightly-batch")[0]


def test_the_token_reaches_nothing_beyond_its_namespace_and_verbs(cluster):
    controls = cluster["controls"]()
    with pytest.raises(ContractError):
        controls.service("Orders!")  # not a DNS label: never sent
    other = cluster["controls"]("kube-system", "kube-system-uid")
    with pytest.raises(KubernetesRequestRefused) as refused:
        other.service("kube-dns")
    assert refused.value.status == 403
    with pytest.raises(KubernetesRequestRefused) as refused:  # no right to pause pods' owner
        controls._patch(controls._ns("/apis/apps/v1", "deployments", "orders-target"), "1",
                        [{"op": "replace", "path": "/spec/replicas", "value": 0}])
    assert refused.value.status == 403
