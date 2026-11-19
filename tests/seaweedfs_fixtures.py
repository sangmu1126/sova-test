"""Two disposable SeaweedFS stores on the kind cluster: one as it starts by
default, one locked.

* ``open`` - ``weed server -s3`` with no identity file: the S3 API accepts
  every request, signed or not (the on-premises object store the team found
  readable without credentials, T9).
* ``locked`` - the same server with an identity file and TLS: an
  administrator, the application and its batch job on ``orders-*`` buckets,
  and another tenant on ``other-*`` buckets; anonymous requests are refused.
  The certificate is issued by a CA made for the module (loopback names).

Both run in a namespace of their own for the test module, as a non-root user
with a read-only root file system; the master, volume and filer servers listen
on the Pod's loopback only. The tests reach the S3 port through ``kubectl
port-forward``. Every key here is a fixed, test-only value for a store that
exists for one module. The image is the release the team's target pinned.

SeaweedFS applies bucket policies to principals written as IAM user ARNs
(``arn:aws:iam::000000000000:user/<identity>``); ``principals`` maps the
logical writers that way. A policy naming a bare identity is stored and
returned, and never applied (measured on 4.52).
"""

import base64
import json
import os
import time

import pytest

IMAGE = ("docker.io/chrislusf/seaweedfs:4.52@sha256:"
         "57877e32bb90073b37dc109b438ba66e84fd5ed9ced5f5c04f56444ef243ce59")
REGION = "us-east-1"
ACCOUNTS = {  # logical name -> (identity, access key, secret, actions)
    "admin": ("admin", "sovaswadmin00001", "sova-sw-admin-test-secret-000000",
              ["Admin", "Read", "Write", "List", "Tagging"]),
    "app": ("app", "sovaswapp0000001", "sova-sw-app-test-secret-00000000",
            [f"{a}:orders-*" for a in ("Read", "Write", "List", "Tagging")]),
    "other": ("batch", "sovaswbatch00001", "sova-sw-batch-test-secret-000000",
              [f"{a}:orders-*" for a in ("Read", "Write", "List", "Tagging")]),
    "outsider": ("outsider", "sovaswoutsider01", "sova-sw-outsider-test-secret-000",
                 [f"{a}:other-*" for a in ("Read", "Write", "List", "Tagging")]),
}


def principal(name):
    return f"arn:aws:iam::000000000000:user/{ACCOUNTS[name][0]}"


def identities():
    return {"identities": [
        {"name": identity, "credentials": [{"accessKey": key, "secretKey": secret}],
         "actions": actions}
        for identity, key, secret, actions in ACCOUNTS.values()]}


def server(name, *, locked):
    labels = {"app": f"seaweedfs-{name}"}
    args = ["-logtostderr=true", "server", "-dir=/data", "-master.dir=/data",
            "-master.peers=none", "-master.volumeSizeLimitMB=16", "-volume.max=100",
            "-master.telemetry=false", "-ip=127.0.0.1", "-ip.bind=127.0.0.1", "-filer",
            "-s3", "-s3.port=8333", "-s3.ip.bind=0.0.0.0", "-s3.iam=false",
            "-s3.port.iceberg=0", "-s3.port.lance=0"]
    mounts = [{"name": "data", "mountPath": "/data"}, {"name": "tmp", "mountPath": "/tmp"}]
    volumes = [{"name": "data", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}}]
    if locked:
        args += ["-s3.config=/etc/seaweedfs-auth/s3.json",
                 "-s3.cert.file=/etc/seaweedfs-tls/server-cert.pem",
                 "-s3.key.file=/etc/seaweedfs-tls/server-key.pem"]
        mounts += [{"name": "auth", "mountPath": "/etc/seaweedfs-auth", "readOnly": True},
                   {"name": "tls", "mountPath": "/etc/seaweedfs-tls", "readOnly": True}]
        volumes += [{"name": "auth", "secret": {"secretName": "seaweedfs-auth",
                                                "defaultMode": 0o444}},
                    {"name": "tls", "secret": {"secretName": "seaweedfs-tls",
                                               "defaultMode": 0o444}}]
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": f"seaweedfs-{name}"},
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                     "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels}, "spec": {
                         "automountServiceAccountToken": False,
                         "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                             "runAsGroup": 1000, "fsGroup": 1000,
                                             "seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [{
                             "name": "seaweedfs", "image": IMAGE,
                             "command": ["/usr/bin/weed"], "args": args,
                             # Each bucket is a collection: one volume at a time, not seven.
                             "env": [{"name": "WEED_MASTER_VOLUME_GROWTH_COPY_1",
                                      "value": "1"}],
                             "ports": [{"containerPort": 8333}],
                             "readinessProbe": {"tcpSocket": {"port": 8333},
                                                "periodSeconds": 2},
                             "resources": {"requests": {"cpu": "50m", "memory": "96Mi"},
                                           "limits": {"memory": "512Mi"}},
                             "securityContext": {"allowPrivilegeEscalation": False,
                                                 "readOnlyRootFilesystem": True,
                                                 "capabilities": {"drop": ["ALL"]}},
                             "volumeMounts": mounts}],
                         "volumes": volumes}}}}


@pytest.fixture(scope="module")
def seaweedfs_stores(tmp_path_factory):
    """``{"open": {...}, "locked": {...}}``: per store its endpoint, a client
    factory (``client(name)`` signed as a logical account, ``client(None)``
    unsigned), and for the locked one its CA (PEM bytes and file)."""
    boto3 = pytest.importorskip("boto3")
    import botocore.config
    from botocore import UNSIGNED

    from kind_support import delete_namespace, kind_namespace, kubectl
    from mysql_pair_business import tls_material
    from redis_business import _forward

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the SeaweedFS tests")
    objects = [server("open", locked=False), server("locked", locked=True)]
    context, namespace, _ = kind_namespace([], [], prefix="seaweedfs")
    forwards = []
    try:
        material = tls_material(namespace)
        secrets = [
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "seaweedfs-tls"},
             "data": {k: base64.b64encode(v).decode() for k, v in material.items()
                      if k != "ca.pem"}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "seaweedfs-auth"},
             "data": {"s3.json": base64.b64encode(json.dumps(identities()).encode()).decode()}}]
        kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps(
            {"apiVersion": "v1", "kind": "List", "items": secrets + objects}))
        for name in ("open", "locked"):
            kubectl(context, "-n", namespace, "rollout", "status",
                    f"deployment/seaweedfs-{name}", "--timeout=240s")
        ca_file = tmp_path_factory.mktemp("seaweedfs-tls") / "ca.pem"
        ca_file.write_bytes(material["ca.pem"])
        ports = {}
        for name in ("open", "locked"):
            process, ports[name] = _forward(context, namespace, f"seaweedfs-{name}", 8333)
            forwards.append(process)
        endpoints = {"open": f"http://127.0.0.1:{ports['open']}",
                     "locked": f"https://localhost:{ports['locked']}"}

        def factory(store):
            verify = str(ca_file) if store == "locked" else None

            def client(name="admin", *, timeout=30):
                config = botocore.config.Config(
                    signature_version=UNSIGNED if name is None else "s3v4",
                    s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=timeout,
                    retries={"mode": "standard", "total_max_attempts": 1})
                credentials = {} if name is None else {
                    "aws_access_key_id": ACCOUNTS[name][1],
                    "aws_secret_access_key": ACCOUNTS[name][2]}
                return boto3.client("s3", endpoint_url=endpoints[store], region_name=REGION,
                                    config=config, verify=verify, **credentials)
            return client

        stores = {name: {"endpoint": endpoints[name], "port": ports[name],
                         "client": factory(name)} for name in ("open", "locked")}
        stores["locked"].update(ca_pem=material["ca.pem"], ca_file=str(ca_file))
        for name, store in stores.items():  # the first write grows the first volume
            admin, deadline = store["client"]("admin", timeout=60), time.monotonic() + 120
            while True:
                try:
                    if "orders-warmup" not in [b["Name"] for b in
                                               admin.list_buckets().get("Buckets", ())]:
                        admin.create_bucket(Bucket="orders-warmup")
                    admin.put_object(Bucket="orders-warmup", Key="ready", Body=b"1")
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(1)
        yield stores
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)
