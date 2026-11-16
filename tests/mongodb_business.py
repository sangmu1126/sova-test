"""Two disposable MongoDB deployments on the kind cluster (authentication on),
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
                     "template": {"metadata": {"labels": labels}, "spec": {"containers": [{
                         "name": "mongo", "image": MONGO, "imagePullPolicy": "IfNotPresent",
                         "args": ["--wiredTigerCacheSizeGB", "0.25", "--quiet"],
                         "env": [{"name": "MONGO_INITDB_ROOT_USERNAME", "value": ADMIN[0]},
                                 {"name": "MONGO_INITDB_ROOT_PASSWORD", "value": ADMIN[1]}],
                         "ports": [{"containerPort": 27017}],
                         "readinessProbe": {"tcpSocket": {"port": 27017}, "periodSeconds": 2},
                         "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                       "limits": {"memory": "768Mi"}}}]}}}}


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
def mongo_servers():
    pymongo = pytest.importorskip("pymongo")
    from kind_support import delete_namespace, kind_namespace, kubectl

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the MongoDB tests")
    context, namespace, _ = kind_namespace([], [server("source"), server("target")],
                                           prefix="mongo")
    forwards = []
    try:
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
        yield {"ports": ports, "admin": clients, "pymongo": pymongo}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


def as_user(servers, side, user):
    return servers["pymongo"].MongoClient(
        "127.0.0.1", servers["ports"][side], username=user, password=PASSWORD,
        authSource="admin", directConnection=True, serverSelectionTimeoutMS=5000)


@pytest.fixture
def mongo_business(mongo_servers):
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
