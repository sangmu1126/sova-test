"""Two disposable MongoDB deployments on the kind cluster (authentication on,
each a single-member replica set: change streams need one),
and an application database on the source for each test: orders with a unique
index on customer and idempotency key, their recorded results, an application
user and a batch job's user (both ``readWrite``), and a reader. The users
exist on both deployments (as an operator creates accounts); on the target
they hold no role. The test reaches the servers through ``kubectl
port-forward``.
"""

import os
import re
import subprocess
import time
from uuid import uuid4

import pytest

MONGO = ("docker.io/library/mongo:8.0.34@sha256:"
         "b9924c330a8bbc5800fe9c69f04a84e221eca7b28fd049a0354147d9908fa142")
ADMIN = ("sova-admin", "sova-mongo-admin-test-secret")
PASSWORD = "sova-mongo-user-test-secret"


def server(side):
    labels = {"app": "mongo", "side": side}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": f"mongo-{side}"},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                     "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels}, "spec": {
                         "initContainers": [{  # mongod accepts only a key file of its own
                             "name": "key", "image": MONGO, "imagePullPolicy": "IfNotPresent",
                             "securityContext": {"runAsUser": 999, "runAsGroup": 999},
                             "command": ["sh", "-c", "cp /key-secret/keyfile /key/keyfile && "
                                         "chmod 400 /key/keyfile"],
                             "volumeMounts": [{"name": "key-secret", "mountPath": "/key-secret",
                                               "readOnly": True},
                                              {"name": "key", "mountPath": "/key"}]}],
                         "containers": [{
                         "name": "mongo", "image": MONGO, "imagePullPolicy": "IfNotPresent",
                         "args": ["--wiredTigerCacheSizeGB", "0.25", "--quiet",
                                  "--replSet", f"rs-{side}", "--oplogSize", "128",
                                  "--keyFile", "/key/keyfile",
                                  # TLS offered (not required): a CA of the test's own
                                  "--tlsMode", "preferTLS",
                                  "--tlsCertificateKeyFile", "/etc/mongo-tls/server.pem",
                                  "--tlsCAFile", "/etc/mongo-tls/ca.pem",
                                  "--tlsAllowConnectionsWithoutCertificates"],
                         "volumeMounts": [{"name": "tls", "mountPath": "/etc/mongo-tls",
                                           "readOnly": True},
                                          {"name": "key", "mountPath": "/key", "readOnly": True}],
                         "env": [{"name": "MONGO_INITDB_ROOT_USERNAME", "value": ADMIN[0]},
                                 {"name": "MONGO_INITDB_ROOT_PASSWORD", "value": ADMIN[1]}],
                         "ports": [{"containerPort": 27017}],
                         "readinessProbe": {"tcpSocket": {"port": 27017}, "periodSeconds": 2},
                         "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                       "limits": {"memory": "768Mi"}}}],
                         "volumes": [{"name": "tls", "secret": {"secretName": "mongo-tls",
                                                                "defaultMode": 0o444}},
                                     {"name": "key-secret", "secret": {
                                         "secretName": "mongo-key", "defaultMode": 0o444}},
                                     {"name": "key", "emptyDir": {}}]}}}}


def _initiate(pymongo, client, name):
    """Make the server a one-member replica set and wait until it is primary."""
    try:
        client.admin.command("replSetInitiate", {"_id": name, "members": [
            {"_id": 0, "host": "localhost:27017"}]})
    except pymongo.errors.OperationFailure as error:
        if error.code != 23:  # AlreadyInitialized
            raise
    deadline = time.monotonic() + 60
    while not client.admin.command("hello").get("isWritablePrimary"):
        if time.monotonic() > deadline:
            raise AssertionError(f"{name} did not become primary")
        time.sleep(0.5)


def _forward(context, namespace, deployment):
    from kind_support import _config

    config = _config(context)
    process = subprocess.Popen(
        ["kubectl", *(["--kubeconfig", config] if config else []), "--context", context,
         "-n", namespace, "port-forward", f"deployment/{deployment}", ":27017"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    found = re.search(r"127\.0\.0\.1:(\d+)", process.stdout.readline())
    if not found:
        process.kill()
        raise AssertionError("port-forward did not start")
    return process, int(found.group(1))


@pytest.fixture(scope="module")
def mongo_servers(tmp_path_factory):
    pymongo = pytest.importorskip("pymongo")
    from kind_support import delete_namespace, kind_namespace, kubectl

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the MongoDB tests")
    import base64
    import json

    from mysql_pair_business import tls_material

    context, namespace, _ = kind_namespace([], [server("source"), server("target")],
                                           prefix="mongo")
    forwards = []
    try:
        material = tls_material(namespace)  # loopback and in-cluster names; Pods wait for it
        bundle = {"ca.pem": material["ca.pem"],
                  "server.pem": material["server-cert.pem"] + material["server-key.pem"]}
        kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "mongo-tls"},
            "data": {k: base64.b64encode(v).decode() for k, v in bundle.items()}}))
        key = base64.b64encode(os.urandom(512))  # the members' shared key (one member each)
        kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "mongo-key"},
            "data": {"keyfile": base64.b64encode(key).decode()}}))
        ca_file = tmp_path_factory.mktemp("mongo-tls") / "ca.pem"
        ca_file.write_bytes(material["ca.pem"])
        ports, clients = {}, {}
        for side in ("source", "target"):
            kubectl(context, "-n", namespace, "rollout", "status", f"deployment/mongo-{side}",
                    "--timeout=300s")
            process, ports[side] = _forward(context, namespace, f"mongo-{side}")
            forwards.append(process)
            clients[side] = pymongo.MongoClient("127.0.0.1", ports[side], username=ADMIN[0],
                                                password=ADMIN[1], authSource="admin",
                                                directConnection=True,
                                                serverSelectionTimeoutMS=5000)
            deadline = time.monotonic() + 90  # the image's first start creates the admin
            while True:
                try:
                    clients[side].admin.command("ping")
                    break
                except pymongo.errors.PyMongoError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(1)
            _initiate(pymongo, clients[side], f"rs-{side}")
        yield {"ports": ports, "admin": clients, "pymongo": pymongo, "ca_file": str(ca_file)}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


def as_user(servers, side, user):
    return servers["pymongo"].MongoClient(
        "127.0.0.1", servers["ports"][side], username=user, password=PASSWORD,
        authSource="admin", directConnection=True, serverSelectionTimeoutMS=5000)


def _business(mongo_servers, shard=False):
    suffix = uuid4().hex[:8]
    names = {k: f"{k}_{suffix}" for k in ("src", "tgt", "scratch", "other")}
    users = {k: f"orders-{k}-{suffix}" for k in ("app", "batch", "reader")}
    for side, client in mongo_servers["admin"].items():
        for key, user in users.items():
            roles = ([{"role": "read" if key == "reader" else "readWrite", "db": names["src"]}]
                     if side == "source" else [])
            client.admin.command("createUser", user, pwd=PASSWORD, roles=roles)
    source = mongo_servers["admin"]["source"][names["src"]]
    source.orders.create_index([("customer", 1), ("idempotency_key", 1)], unique=True,
                               name="one_order_per_key")
    if shard:  # the results spread over the shards by a hashed key; orders stay on one
        mongo_servers["admin"]["source"].admin.command(
            "shardCollection", f"{names['src']}.idempotency_results", key={"_id": "hashed"})
    app = as_user(mongo_servers, "source", users["app"])[names["src"]]
    for i in range(1, 41):  # business before the handoff
        app.orders.insert_one({"_id": i, "customer": f"customer-{i % 7}",
                               "idempotency_key": f"key-{i}", "amount": i * 10})
        app.idempotency_results.insert_one({"_id": f"customer-{i % 7}:key-{i}",
                                            "result": "confirmed"})
    other = mongo_servers["admin"]["source"][names["other"]]
    other.shop.insert_one({"_id": 1, "tenant": "other"})
    yield {**mongo_servers, "names": names, "users": users,
           "writers": tuple(f"{users[k]}@admin" for k in ("app", "batch"))}
    for client in mongo_servers["admin"].values():
        for database in names.values():
            client.drop_database(database)
        for user in client.admin.command("usersInfo")["users"]:
            if user["user"].endswith(suffix):
                client.admin.command("dropUser", user["user"])


@pytest.fixture
def mongo_business(mongo_servers):
    yield from _business(mongo_servers)



def _sharded_objects(namespace):
    """A config server, two shards (each a one-member replica set) and a mongos,
    authenticated by one key file; a replica set as the target."""
    def daemon(name, args, port):
        labels = {"app": name}
        return [
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
             "spec": {"selector": labels, "ports": [{"port": port}]}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
             "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                      "selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": {
                          "initContainers": [{
                              "name": "key", "image": MONGO, "imagePullPolicy": "IfNotPresent",
                              "securityContext": {"runAsUser": 999, "runAsGroup": 999},
                              "command": ["sh", "-c", "cp /key-secret/keyfile /key/keyfile && "
                                          "chmod 400 /key/keyfile"],
                              "volumeMounts": [{"name": "key-secret",
                                                "mountPath": "/key-secret", "readOnly": True},
                                               {"name": "key", "mountPath": "/key"}]}],
                          "containers": [{
                              "name": "mongo", "image": MONGO, "imagePullPolicy": "IfNotPresent",
                              "securityContext": {"runAsUser": 999, "runAsGroup": 999},
                              "command": args + ["--port", str(port), "--bind_ip_all",
                                                 "--keyFile", "/key/keyfile", "--quiet"],
                              "ports": [{"containerPort": port}],
                              "readinessProbe": {"tcpSocket": {"port": port},
                                                 "periodSeconds": 2},
                              "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                            "limits": {"memory": "640Mi"}},
                              "volumeMounts": [{"name": "key", "mountPath": "/key",
                                                "readOnly": True},
                                               {"name": "data", "mountPath": "/data/db"}]}],
                          "volumes": [{"name": "key-secret", "secret": {
                              "secretName": "mongo-key", "defaultMode": 0o444}},
                              {"name": "key", "emptyDir": {}},
                              {"name": "data", "emptyDir": {}}]}}}}]

    host = f"{namespace}.svc.cluster.local"
    cache = ["--wiredTigerCacheSizeGB", "0.25"]
    return (daemon("mongo-cfg", ["mongod", "--configsvr", "--replSet", "cfg", *cache], 27019)
            + daemon("mongo-sha", ["mongod", "--shardsvr", "--replSet", "sha", *cache], 27018)
            + daemon("mongo-shb", ["mongod", "--shardsvr", "--replSet", "shb", *cache], 27018)
            + daemon("mongo-router", ["mongos", "--configdb", f"cfg/mongo-cfg.{host}:27019"],
                     27017))


@pytest.fixture(scope="module")
def mongo_sharded_servers(tmp_path_factory):
    """A sharded cluster as the source (two shards behind a mongos) and a
    replica set as the target - a sharded deployment brought onto one set."""
    pymongo = pytest.importorskip("pymongo")
    import base64
    import json

    from kind_support import delete_namespace, kind_namespace, kubectl

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the MongoDB tests")
    from mysql_pair_business import tls_material

    context, namespace, _ = kind_namespace([], [server("target")], prefix="mongo-sharded")
    forwards = []
    try:
        material = tls_material(namespace)  # the target's TLS (offered, not required)
        bundle = {"ca.pem": material["ca.pem"],
                  "server.pem": material["server-cert.pem"] + material["server-key.pem"]}
        key = base64.b64encode(os.urandom(512))
        for name, data in (("mongo-tls", bundle), ("mongo-key", {"keyfile": key})):
            kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps({
                "apiVersion": "v1", "kind": "Secret", "metadata": {"name": name},
                "data": {k: base64.b64encode(v).decode() for k, v in data.items()}}))
        kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "List", "items": _sharded_objects(namespace)}))
        host = f"{namespace}.svc.cluster.local"

        def shell(deployment, port, script):  # the localhost exception: no user yet
            return kubectl(context, "-n", namespace, "exec", f"deployment/{deployment}", "--",
                           "mongosh", "--quiet", "--port", str(port), "--eval", script)

        for deployment in ("mongo-cfg", "mongo-sha", "mongo-shb", "mongo-target"):
            kubectl(context, "-n", namespace, "rollout", "status", f"deployment/{deployment}",
                    "--timeout=170s")
        for deployment, rs, port, extra in (("mongo-cfg", "cfg", 27019, "configsvr: true, "),
                                            ("mongo-sha", "sha", 27018, ""),
                                            ("mongo-shb", "shb", 27018, "")):
            shell(deployment, port, f"rs.initiate({{_id: '{rs}', {extra}members: [{{_id: 0, "
                                    f"host: '{deployment}.{host}:{port}'}}]}})")
            deadline = time.monotonic() + 90
            while "true" not in shell(deployment, port, "db.hello().isWritablePrimary"):
                assert time.monotonic() < deadline, f"{rs} did not become primary"
                time.sleep(1)
        # the router starts once the config server's set is up
        kubectl(context, "-n", namespace, "rollout", "status", "deployment/mongo-router",
                "--timeout=170s")
        deadline = time.monotonic() + 90
        while True:  # the router reaches the config server
            try:
                shell("mongo-router", 27017, "db.getSiblingDB('admin').createUser({user: "
                                             f"'{ADMIN[0]}', pwd: '{ADMIN[1]}', roles: ['root']}})")
                break
            except AssertionError:
                assert time.monotonic() < deadline, "the router did not take the first user"
                time.sleep(2)
        ports, clients = {}, {}
        for side, deployment in (("source", "mongo-router"), ("target", "mongo-target")):
            process, ports[side] = _forward(context, namespace, deployment)
            forwards.append(process)
            clients[side] = pymongo.MongoClient("127.0.0.1", ports[side], username=ADMIN[0],
                                                password=ADMIN[1], authSource="admin",
                                                directConnection=True,
                                                serverSelectionTimeoutMS=5000)
        for rs in ("sha", "shb"):
            clients["source"].admin.command("addShard", f"{rs}/mongo-{rs}.{host}:27018")
        deadline = time.monotonic() + 90
        while True:  # the target's first start creates its administrator
            try:
                clients["target"].admin.command("ping")
                break
            except pymongo.errors.PyMongoError:
                assert time.monotonic() < deadline
                time.sleep(1)
        _initiate(pymongo, clients["target"], "rs-target")
        ca_file = tmp_path_factory.mktemp("mongo-tls") / "ca.pem"
        ca_file.write_bytes(material["ca.pem"])
        yield {"ports": ports, "admin": clients, "pymongo": pymongo, "ca_file": str(ca_file)}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


@pytest.fixture
def mongo_sharded_business(mongo_sharded_servers):
    yield from _business(mongo_sharded_servers, shard=True)
