"""Two MySQL servers for change-capture handoffs: the source and the target are
different servers, as they are when an application leaves a managed database.
Both run in a namespace of the test module's own (row-based binary log with
GTIDs, distinct server IDs); the test reaches them through ``kubectl
port-forward`` and the target reaches the source through its Service, as a
replica would. The source has a replication account, what an operator gives
for the handoff.

Each test gets the order business of ``mysql_business`` on the source and the
writer accounts on both servers (an account exists where the application will
log in; its rights there are the adapter's to give).
"""

import os
import re
import subprocess
import time
from uuid import uuid4

import pytest
from mysql_business import PASSWORD, SCHEMA

MYSQL = "mysql:8.4.13"
ROOT = "sova-mysql-pair-test-secret"
REPLICATION = ("sova_repl", "sova-replication-test-secret")


def server(side, server_id, gtid=True):
    labels = {"app": "mysql", "side": side}
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": f"mysql-{side}"},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                  "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {"containers": [{
                      "name": "mysql", "image": MYSQL, "imagePullPolicy": "IfNotPresent",
                      "args": [f"--server-id={server_id}", "--skip-name-resolve",
                               "--innodb-buffer-pool-size=32M", "--performance-schema=OFF",
                               "--max-connections=100",
                               *(["--gtid-mode=ON", "--enforce-gtid-consistency=ON"]
                                 if gtid else ["--gtid-mode=OFF"]),
                               "--skip-replica-start",
                               # TLS offered (not required): a CA of the test's own
                               "--ssl-ca=/etc/mysql-tls/ca.pem",
                               "--ssl-cert=/etc/mysql-tls/server-cert.pem",
                               "--ssl-key=/etc/mysql-tls/server-key.pem"],
                      "volumeMounts": [{"name": "tls", "mountPath": "/etc/mysql-tls",
                                        "readOnly": True}],
                      "env": [{"name": "MYSQL_ROOT_PASSWORD", "value": ROOT},
                              {"name": "MYSQL_ROOT_HOST", "value": "%"}],
                      "ports": [{"containerPort": 3306}],
                      "readinessProbe": {"exec": {"command": [
                          "mysqladmin", "ping", "-h127.0.0.1", "-uroot", f"-p{ROOT}"]},
                          "periodSeconds": 2},
                      "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                    "limits": {"memory": "512Mi"}}}],
                      "volumes": [{"name": "tls", "secret": {"secretName": "mysql-tls",
                                                             "defaultMode": 0o444}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"mysql-{side}"},
         "spec": {"selector": labels, "ports": [{"port": 3306, "targetPort": 3306}]}}]


def tls_material(namespace):
    """A CA and one server certificate for both servers: loopback (the test's
    port-forward) and each server's name inside the cluster (a replica's)."""
    from datetime import UTC, datetime, timedelta
    from ipaddress import ip_address

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.now(UTC)

    def certificate(subject, issuer, public, signer, *, ca, names=()):
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                   .public_key(public).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if names:
            builder = builder.add_extension(x509.SubjectAlternativeName(list(names)),
                                            critical=False)
        return builder.sign(signer, hashes.SHA256())

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sova mysql test CA")])
    ca = certificate(ca_name, ca_name, ca_key.public_key(), ca_key, ca=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [x509.IPAddress(ip_address("127.0.0.1")), x509.DNSName("localhost"),
             *(x509.DNSName(f"mysql-{side}.{namespace}.svc.cluster.local")
               for side in ("source", "target"))]
    server_cert = certificate(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mysql")]),
                              ca_name, key.public_key(), ca_key, ca=False, names=names)
    pem = serialization.Encoding.PEM
    return {"ca.pem": ca.public_bytes(pem), "server-cert.pem": server_cert.public_bytes(pem),
            "server-key.pem": key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption())}


def _forward(context, namespace, deployment):
    from kind_support import _config

    config = _config(context)
    process = subprocess.Popen(
        ["kubectl", *(["--kubeconfig", config] if config else []), "--context", context,
         "-n", namespace, "port-forward", f"deployment/{deployment}", ":3306"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    found = re.search(r"127\.0\.0\.1:(\d+)", process.stdout.readline())
    if not found:
        process.kill()
        raise AssertionError("port-forward did not start")
    return process, int(found.group(1))


@pytest.fixture(scope="module")
def mysql_pair(tmp_path_factory):
    yield from _pair(tmp_path_factory)


@pytest.fixture(scope="module")
def mysql_pair_gtid_off_source(tmp_path_factory):
    """As ``mysql_pair``, the source with GTIDs off (MySQL's own default) and the
    target with them on."""
    yield from _pair(tmp_path_factory, source_gtid=False)


def _pair(tmp_path_factory, source_gtid=True):
    pymysql = pytest.importorskip("pymysql")
    from kind_support import delete_namespace, kind_namespace, kubectl

    if not os.environ.get("SOVA_TEST_KUBE_CONTEXT"):
        pytest.skip("SOVA_TEST_KUBE_CONTEXT is required for the two-server MySQL tests")
    import base64
    import json

    context, namespace, _ = kind_namespace([], [*server("source", 101, source_gtid),
                                                 *server("target", 102)], prefix="mysqlpair")
    forwards = []
    try:
        material = tls_material(namespace)  # the Pods wait for it to mount
        kubectl(context, "-n", namespace, "apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "mysql-tls"},
            "data": {k: base64.b64encode(v).decode() for k, v in material.items()}}))
        ca_file = tmp_path_factory.mktemp("mysql-tls") / "ca.pem"
        ca_file.write_bytes(material["ca.pem"])
        for side in ("source", "target"):
            kubectl(context, "-n", namespace, "rollout", "status", f"deployment/mysql-{side}",
                    "--timeout=300s")
        admin = {}
        for side in ("source", "target"):
            process, port = _forward(context, namespace, f"mysql-{side}")
            forwards.append(process)
            admin[side] = {"host": "127.0.0.1", "port": port, "user": "root", "password": ROOT,
                           "connect_timeout": 5}
            deadline = time.monotonic() + 60
            while True:
                try:
                    pymysql.connect(**admin[side]).close()
                    break
                except pymysql.err.OperationalError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(1)
        with pymysql.connect(**admin["source"]) as conn, conn.cursor() as cur:
            cur.execute(f"CREATE USER '{REPLICATION[0]}'@'%' IDENTIFIED BY '{REPLICATION[1]}'")
            cur.execute(f"GRANT REPLICATION SLAVE ON *.* TO '{REPLICATION[0]}'@'%'")
        yield {"admin": admin, "pymysql": pymysql, "ca_file": str(ca_file),
               "source_endpoint": (f"mysql-source.{namespace}.svc.cluster.local", 3306)}
    finally:
        for process in forwards:
            process.terminate()
        delete_namespace(context, namespace)


@pytest.fixture
def mysql_pair_business(mysql_pair):
    yield from _pair_business(mysql_pair)


@pytest.fixture
def mysql_gtid_off_business(mysql_pair_gtid_off_source):
    yield from _pair_business(mysql_pair_gtid_off_source)


def _pair_business(mysql_pair):
    from sova.adapters.mysql_offline_copy import statements

    pymysql, admin = mysql_pair["pymysql"], mysql_pair["admin"]
    suffix = uuid4().hex[:10]
    names = {k: f"{k}_{suffix}" for k in ("src", "tgt", "scratch", "other")}
    app, batch = f"orders_app_{suffix}@%", f"orders_batch_{suffix}@%"

    def side_of(database):
        return "target" if database == names["tgt"] else "source"

    def connect(database):
        """The administrative connection to the server holding ``database``."""
        return pymysql.connect(**admin[side_of(database)], autocommit=False)

    def connect_as(database, writer):
        user = writer.rsplit("@", 1)[0]
        return pymysql.connect(**{**admin[side_of(database)], "user": user,
                                  "password": PASSWORD}, database=database, autocommit=False)

    def quoted(writer):
        user, host = writer.rsplit("@", 1)
        return f"'{user}'@'{host}'"

    for side in ("source", "target"):
        with pymysql.connect(**admin[side]) as conn, conn.cursor() as cur:
            for writer in (app, batch):
                cur.execute(f"CREATE USER {quoted(writer)} IDENTIFIED BY '{PASSWORD}'")
            conn.commit()
    with pymysql.connect(**admin["source"]) as conn, conn.cursor() as cur:
        for key in ("src", "scratch", "other"):
            cur.execute(f"CREATE DATABASE `{names[key]}`")
        cur.execute(f"USE `{names['src']}`")
        for statement in statements(SCHEMA):
            cur.execute(statement)
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{names['src']}`.* TO "
                    f"{quoted(app)}")
        cur.execute(f"GRANT INSERT ON `{names['src']}`.`orders` TO {quoted(batch)}")
        conn.commit()
    with connect_as(names["src"], app) as conn, conn.cursor() as cur:
        for i in range(1, 51):
            cur.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                        (i, f"customer-{i % 7}", f"key-{i}", i * 10))
            cur.execute("INSERT INTO idempotency_results VALUES (%s, %s, 'confirmed')",
                        (f"customer-{i % 7}", f"key-{i}"))
            cur.execute("INSERT INTO work_items VALUES (%s, %s, 'done')", (i, i))
        for _ in range(10):
            cur.execute("INSERT INTO order_numbers () VALUES ()")
        cur.execute("DELETE FROM order_numbers WHERE n > 8")
        conn.commit()
    yield {"names": names, "app": app, "batch": batch, "connect": connect,
           "connect_as": connect_as, "quoted": quoted, "admin": admin["source"],
           "admins": admin, "pymysql": pymysql, "side_of": side_of,
           "source_endpoint": mysql_pair["source_endpoint"], "ca_file": mysql_pair["ca_file"]}
    for side in ("target", "source"):
        with pymysql.connect(**admin[side]) as conn, conn.cursor() as cur:
            if side == "target":
                cur.execute("SET GLOBAL replica_exec_mode = 'STRICT'")  # a lockless sync's
                cur.execute("SET GLOBAL replica_parallel_workers = DEFAULT")
                cur.execute("SHOW REPLICA STATUS")
                columns = [d[0] for d in cur.description or ()]
                for row in cur.fetchall():
                    channel = dict(zip(columns, row)).get("Channel_Name")
                    if channel and channel.startswith("sova_"):
                        cur.execute(f"STOP REPLICA FOR CHANNEL '{channel}'")
                        cur.execute(f"RESET REPLICA ALL FOR CHANNEL '{channel}'")
            cur.execute("XA RECOVER")
            for row in cur.fetchall():
                xid = row[3].decode() if isinstance(row[3], bytes) else row[3]
                if xid.startswith("sova-conformance-"):
                    cur.execute(f"XA ROLLBACK '{xid}'")
            for database in names.values():
                cur.execute(f"DROP DATABASE IF EXISTS `{database}`")
            for writer in (app, batch):
                cur.execute(f"DROP USER IF EXISTS {quoted(writer)}")
            conn.commit()
