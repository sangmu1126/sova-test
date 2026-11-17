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
tls-port 6380
tls-cert-file /etc/valkey-tls/server-cert.pem
tls-key-file /etc/valkey-tls/server-key.pem
tls-ca-cert-file /etc/valkey-tls/ca.pem
tls-auth-clients no
save ""
appendonly no
user default off
user {ADMIN[0]} on >{ADMIN[1]} ~* &* +@all
"""


def replica_config(side):
    """A replica of ``valkey-{side}``, logging in to it as the administrator."""
    return CONFIG + (f"replicaof valkey-{side} 6379\nmasteruser {ADMIN[0]}\n"
                     f"masterauth {ADMIN[1]}\n")


def sentinel_config():
    """One Sentinel watching both primaries (``sova-source``, ``sova-target``),
    logging in to them as the administrator; no failover during a test."""
    lines = ["port 26379", "sentinel resolve-hostnames yes"]
    for side in ("source", "target"):
        name = f"sova-{side}"
        lines += [f"sentinel monitor {name} valkey-{side} 6379 1",
                  f"sentinel auth-user {name} {ADMIN[0]}",
                  f"sentinel auth-pass {name} {ADMIN[1]}",
                  f"sentinel down-after-milliseconds {name} 600000",
                  f"sentinel failover-timeout {name} 1200000"]
    return "\n".join(lines) + "\n"


def sentinel():
    labels = {"app": "valkey-sentinel"}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "valkey-sentinel"},
         "data": {"sentinel.conf": sentinel_config()}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "valkey-sentinel"},
         "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 999,
                                          "runAsGroup": 999,
                                          "seccompProfile": {"type": "RuntimeDefault"}},
                      "containers": [{
                          "name": "sentinel", "image": VALKEY, "imagePullPolicy": "IfNotPresent",
                          # Sentinel rewrites its configuration: a writable copy
                          "command": ["sh", "-c", "cp /config/sentinel.conf /data/sentinel.conf"
                                      " && exec valkey-sentinel /data/sentinel.conf"],
                          "ports": [{"containerPort": 26379}],
                          "readinessProbe": {"tcpSocket": {"port": 26379}, "periodSeconds": 1},
                          "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
                          "securityContext": {"allowPrivilegeEscalation": False,
                                              "readOnlyRootFilesystem": True,
                                              "capabilities": {"drop": ["ALL"]}},
                          "volumeMounts": [{"name": "config", "mountPath": "/config"},
                                           {"name": "data", "mountPath": "/data"}]}],
                      "volumes": [{"name": "config",
                                   "configMap": {"name": "valkey-sentinel"}},
                                  {"name": "data", "emptyDir": {}}]}}}}]


def server(side, role="primary"):
    labels = {"app": "valkey", "side": side, "role": role}
    name = f"valkey-{side}" + ("" if role == "primary" else "-replica")
    config = "valkey" if role == "primary" else f"valkey-replica-{side}"
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
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
                                              {"name": "tls", "mountPath": "/etc/valkey-tls",
                                               "readOnly": True},
                                              {"name": "data", "mountPath": "/data"}]}],
                         "volumes": [{"name": "config", "configMap": {"name": config}},
                                     {"name": "tls", "secret": {"secretName": "valkey-tls",
                                                                "defaultMode": 0o444}},
                                     {"name": "data", "emptyDir": {}}]}}}}


def _forward(context, namespace, deployment, port=6379, local=""):
    """``kubectl port-forward`` to ``port``, on ``local`` (default: any free port)."""
    from kind_support import _config

    config = _config(context)
    process = subprocess.Popen(
        ["kubectl", *(["--kubeconfig", config] if config else []), "--context", context,
         "-n", namespace, "port-forward", f"deployment/{deployment}", f"{local}:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = process.stdout.readline()
    found = re.search(r"127\.0\.0\.1:(\d+)", line)
    if not found:
        process.kill()
        raise AssertionError(f"port-forward did not start: {line!r}")
    return process, int(found.group(1))


def _servers(tmp_path_factory, *, replicated, sentinels=False):
    pytest.importorskip("redis")
    import base64

    import redis

    from kind_support import delete_namespace, kind_namespace, kubectl
    from mysql_pair_business import tls_material

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the Redis-protocol tests")
    objects = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "valkey"},
                "data": {"valkey.conf": CONFIG}}, server("source"), server("target")]
    if replicated:  # each primary reachable by name, and a replica following it
        for side in ("source", "target"):
            objects += [
                {"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"valkey-{side}"},
                 "spec": {"selector": {"app": "valkey", "side": side, "role": "primary"},
                          "ports": [{"port": 6379}]}},
                {"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": f"valkey-replica-{side}"},
                 "data": {"valkey.conf": replica_config(side)}},
                server(side, "replica")]
    if sentinels:
        objects += sentinel()
    context, namespace, _ = kind_namespace([], objects, prefix="valkey")
    forwards = []
    try:
        material = tls_material(namespace)  # loopback and in-cluster names; Pods wait for it
        kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "valkey-tls"},
            "data": {k: base64.b64encode(v).decode() for k, v in material.items()}}))
        ca_file = tmp_path_factory.mktemp("valkey-tls") / "ca.pem"
        ca_file.write_bytes(material["ca.pem"])
        names = {side: [f"valkey-{side}"] + ([f"valkey-{side}-replica"] if replicated else [])
                 for side in ("source", "target")}
        for deployment in sum(names.values(), []) + (["valkey-sentinel"] if sentinels else []):
            kubectl(context, "-n", namespace, "rollout", "status", f"deployment/{deployment}",
                    "--timeout=180s")
        ports, tls_ports, replica_ports = {}, {}, {}
        for side in ("source", "target"):
            process, ports[side] = _forward(context, namespace, f"valkey-{side}")
            forwards.append(process)
            process, tls_ports[side] = _forward(context, namespace, f"valkey-{side}", 6380)
            forwards.append(process)
            replica_ports[side] = []
            for deployment in names[side][1:]:
                process, port = _forward(context, namespace, deployment)
                forwards.append(process)
                replica_ports[side].append(port)

        def admin(port):
            return redis.Redis(host="127.0.0.1", port=port, username=ADMIN[0],
                               password=ADMIN[1], socket_timeout=10)

        clients = {side: admin(port) for side, port in ports.items()}
        replicas = {side: [admin(port) for port in found]
                    for side, found in replica_ports.items()}
        for client in [*clients.values(), *sum(replicas.values(), [])]:
            deadline = time.monotonic() + 30
            while True:
                try:
                    client.ping()
                    break
                except redis.ConnectionError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.3)
        for side, found in replicas.items():  # each replica has caught up with its primary
            for client in found:
                deadline = time.monotonic() + 60
                while client.info("replication").get("master_link_status") != "up":
                    if time.monotonic() > deadline:
                        raise AssertionError(f"the {side} replica did not reach its primary")
                    time.sleep(0.5)
        watching = None
        if sentinels:  # the Sentinel has found each primary's replica
            process, port = _forward(context, namespace, "valkey-sentinel", 26379)
            forwards.append(process)
            watching = redis.Redis(host="127.0.0.1", port=port, socket_timeout=10)
            deadline = time.monotonic() + 90
            while not all(len(watching.sentinel_slaves(f"sova-{side}")) >= len(replicas[side])
                          for side in ("source", "target")):
                if time.monotonic() > deadline:
                    raise AssertionError("the Sentinel did not find the replicas")
                time.sleep(1)
        yield {"ports": ports, "tls_ports": tls_ports, "ca_file": str(ca_file),
               "admin": clients, "redis": redis, "replica_ports": replica_ports,
               "replicas": replicas, "sentinel": watching}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


@pytest.fixture(scope="module")
def redis_servers(tmp_path_factory):
    yield from _servers(tmp_path_factory, replicated=False)


@pytest.fixture(scope="module")
def redis_replicated_servers(tmp_path_factory):
    """As ``redis_servers``, and each primary with a replica following it."""
    yield from _servers(tmp_path_factory, replicated=True)


@pytest.fixture(scope="module")
def redis_sentinel_servers(tmp_path_factory):
    """As ``redis_replicated_servers``, and a Sentinel watching both primaries."""
    yield from _servers(tmp_path_factory, replicated=True, sentinels=True)


def as_user(servers, side, name):
    """A user's own login, tried once (as the worker's settings make it)."""
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    return servers["redis"].Redis(host="127.0.0.1", port=servers["ports"][side], username=name,
                                  password=PASSWORD, socket_timeout=10,
                                  retry=Retry(NoBackoff(), 0))


def _business(redis_servers):
    suffix = uuid4().hex[:8]
    prefix = f"board-{suffix}:"
    users = {k: f"board-{k}-{suffix}" for k in ("app", "batch", "reader")}
    source = redis_servers["admin"]["source"]
    rules = {"app": ["+@all", "-@admin", "-@dangerous"], "batch": ["+@read", "+@write"],
             "reader": ["+@read"]}
    for key, name in users.items():  # on every node: ACL users are kept per node
        for node in [source, *redis_servers["replicas"]["source"]]:
            node.execute_command("ACL", "SETUSER", name, "reset", "on", f">{PASSWORD}",
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
        for key in client.scan_iter(match="__sova:sync:*"):
            client.delete(key)
    for client in [*redis_servers["admin"].values(),
                   *sum(redis_servers["replicas"].values(), [])]:
        for name in client.execute_command("ACL", "USERS"):
            name = name.decode()
            if name.endswith(suffix):
                client.execute_command("ACL", "DELUSER", name)


@pytest.fixture
def redis_business(redis_servers):
    yield from _business(redis_servers)


@pytest.fixture
def redis_replicated_business(redis_replicated_servers):
    yield from _business(redis_replicated_servers)


@pytest.fixture
def redis_sentinel_business(redis_sentinel_servers):
    yield from _business(redis_sentinel_servers)


REDIS = ("docker.io/library/redis:8.2.2@sha256:"
         "4521b581dbddea6e7d81f8fe95ede93f5648aaa66a9dacd581611bf6fe7527bd")
PLAIN_CONFIG = f"""port 6379
save ""
appendonly no
user default off
user {ADMIN[0]} on >{ADMIN[1]} ~* &* +@all
"""


def _plain_server(name, image, command):
    labels = {"app": name}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels}, "spec": {
                         "securityContext": {"runAsNonRoot": True, "runAsUser": 999,
                                             "runAsGroup": 999,
                                             "seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [{
                             "name": "server", "image": image, "imagePullPolicy": "IfNotPresent",
                             "command": [command, "/etc/server/server.conf"],
                             "ports": [{"containerPort": 6379}],
                             "readinessProbe": {"tcpSocket": {"port": 6379}, "periodSeconds": 1},
                             "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}},
                             "securityContext": {"allowPrivilegeEscalation": False,
                                                 "readOnlyRootFilesystem": True,
                                                 "capabilities": {"drop": ["ALL"]}},
                             "volumeMounts": [{"name": "config", "mountPath": "/etc/server"},
                                              {"name": "data", "mountPath": "/data"}]}],
                         "volumes": [{"name": "config", "configMap": {"name": "plain-server"}},
                                     {"name": "data", "emptyDir": {}}]}}}}


@pytest.fixture(scope="module")
def redis_to_valkey_servers():
    """A Redis 8 server as the source and a Valkey 9 server as the target: the
    move many make since Redis changed its license."""
    pytest.importorskip("redis")
    import redis

    from kind_support import delete_namespace, kind_namespace, kubectl

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the Redis-protocol tests")
    objects = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "plain-server"},
                "data": {"server.conf": PLAIN_CONFIG}},
               _plain_server("redis-source", REDIS, "redis-server"),
               _plain_server("valkey-target", VALKEY, "valkey-server")]
    context, namespace, _ = kind_namespace([], objects, prefix="redis2valkey")
    forwards = []
    try:
        ports = {}
        for side, deployment in (("source", "redis-source"), ("target", "valkey-target")):
            kubectl(context, "-n", namespace, "rollout", "status", f"deployment/{deployment}",
                    "--timeout=300s")
            process, ports[side] = _forward(context, namespace, deployment)
            forwards.append(process)
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
        yield {"ports": ports, "admin": clients, "redis": redis,
               "replicas": {"source": [], "target": []}}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


@pytest.fixture
def redis_to_valkey_business(redis_to_valkey_servers):
    yield from _business(redis_to_valkey_servers)
