"""Two disposable Redis-protocol servers (Valkey) on the kind cluster, and an
application's keyspace on the source for each test.

The servers run in a namespace of their own for the test module, with the
``default`` user off and an administrative user (what Sova would be given);
the test reaches them through ``kubectl port-forward``. Each test gets its own
key prefix and ACL users (the application, a batch job, a reader) and
business already written: sessions that expire, carts, a counter, a
leaderboard and an event stream with a consumer group and pending entries.
"""

import json
import os
import re
import subprocess
import time
from uuid import uuid4

import pytest

VALKEY = ("docker.io/valkey/valkey:9.1.3@sha256:"
          "3add1a336a920ea54770a001ab6feb3da5fceea96a40373a3a2cd469b815f0b7")
ADMIN = ("sova-admin", "sova-admin-test-secret")
PASSWORD = "sova-redis-user-test-secret"
CONFIG = f"""port 6379
save ""
appendonly no
user default off
user {ADMIN[0]} on >{ADMIN[1]} ~* &* +@all
"""


def server(side):
    labels = {"app": "valkey", "side": side}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": f"valkey-{side}"},
            "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels}, "spec": {
                         "securityContext": {"runAsNonRoot": True, "runAsUser": 999,
                                             "runAsGroup": 999,
                                             "seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [{
                             "name": "valkey", "image": VALKEY, "imagePullPolicy": "IfNotPresent",
                             "command": ["valkey-server", "/etc/valkey/valkey.conf"],
                             "ports": [{"containerPort": 6379}],
                             "readinessProbe": {"tcpSocket": {"port": 6379}, "periodSeconds": 1},
                             "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
                             "securityContext": {"allowPrivilegeEscalation": False,
                                                 "readOnlyRootFilesystem": True,
                                                 "capabilities": {"drop": ["ALL"]}},
                             "volumeMounts": [{"name": "config", "mountPath": "/etc/valkey"},
                                              {"name": "data", "mountPath": "/data"}]}],
                         "volumes": [{"name": "config", "configMap": {"name": "valkey"}},
                                     {"name": "data", "emptyDir": {}}]}}}}


def _forward(context, namespace, deployment):
    from kind_support import _config

    config = _config(context)
    process = subprocess.Popen(
        ["kubectl", *(["--kubeconfig", config] if config else []), "--context", context,
         "-n", namespace, "port-forward", f"deployment/{deployment}", ":6379"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = process.stdout.readline()
    found = re.search(r"127\.0\.0\.1:(\d+)", line)
    if not found:
        process.kill()
        raise AssertionError(f"port-forward did not start: {line!r}")
    return process, int(found.group(1))


@pytest.fixture(scope="module")
def redis_servers():
    pytest.importorskip("redis")
    import redis

    from kind_support import delete_namespace, kind_namespace, kubectl

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the Redis-protocol tests")
    configmap = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "valkey"},
                 "data": {"valkey.conf": CONFIG}}
    context, namespace, _ = kind_namespace([], [configmap, server("source"), server("target")],
                                           prefix="valkey")
    forwards = []
    try:
        for side in ("source", "target"):
            kubectl(context, "-n", namespace, "rollout", "status", f"deployment/valkey-{side}",
                    "--timeout=180s")
        ports = {}
        for side in ("source", "target"):
            process, port = _forward(context, namespace, f"valkey-{side}")
            forwards.append(process)
            ports[side] = port
        clients = {side: redis.Redis(host="127.0.0.1", port=port, username=ADMIN[0],
                                     password=ADMIN[1], socket_timeout=10)
                   for side, port in ports.items()}
        for client in clients.values():
            deadline = time.monotonic() + 30
            while True:
                try:
                    client.ping()
                    break
                except redis.ConnectionError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.3)
        yield {"ports": ports, "admin": clients, "redis": redis}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


def as_user(servers, side, name):
    return servers["redis"].Redis(host="127.0.0.1", port=servers["ports"][side], username=name,
                                  password=PASSWORD, socket_timeout=10)


@pytest.fixture
def redis_business(redis_servers):
    suffix = uuid4().hex[:8]
    prefix = f"board-{suffix}:"
    users = {k: f"board-{k}-{suffix}" for k in ("app", "batch", "reader")}
    source = redis_servers["admin"]["source"]
    rules = {"app": ["+@all", "-@admin", "-@dangerous"], "batch": ["+@read", "+@write"],
             "reader": ["+@read"]}
    for key, name in users.items():
        source.execute_command("ACL", "SETUSER", name, "reset", "on", f">{PASSWORD}",
                               f"~{prefix}*", "resetchannels", *rules[key])
    app = as_user(redis_servers, "source", users["app"])
    for i in range(1, 21):  # business before the handoff
        app.hset(f"{prefix}session:{i}", mapping={"user": f"u{i}", "cart": str(i % 4)})
        app.expire(f"{prefix}session:{i}", 3600 + i)
        app.rpush(f"{prefix}cart:{i % 4}", *[f"item-{i}-{n}" for n in range(3)])
        app.incr(f"{prefix}orders:issued")
        app.zadd(f"{prefix}leaderboard", {f"u{i}": i * 10.5})
        app.xadd(f"{prefix}events", {"kind": "login", "user": f"u{i}"})
    app.xgroup_create(f"{prefix}events", "mailer", id="0")
    app.xreadgroup("mailer", "worker-1", {f"{prefix}events": ">"}, count=5)  # 5 pending
    app.set(f"{prefix}flag", json.dumps({"maintenance": False}))
    app.close()
    yield {**redis_servers, "prefix": prefix, "users": users}
    for client in redis_servers["admin"].values():
        keys = list(client.scan_iter(match=prefix + "*", count=500))
        if keys:
            client.delete(*keys)
        for name in client.execute_command("ACL", "USERS"):
            name = name.decode()
            if name.endswith(suffix):
                client.execute_command("ACL", "DELUSER", name)
        for key in client.scan_iter(match="__sova:sync:*"):
            client.delete(key)
