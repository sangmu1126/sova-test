"""Real business HTTPS/NodePort fixture for the existing Pod recovery journey."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from qualification_pods import NAMESPACE, app_pod, command, secret, wait_for

from sova.qualification.postgres_orders import observe_orders
from sova.qualification.requests import RequestJournal, reconcile


def web_certificates():
    now = datetime.now(UTC)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable HTTPS order CA")])
    ca = (x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
          .not_valid_after(now+timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
          .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), True)
          .sign(ca_key, hashes.SHA256()))
    server = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "orders.fixture")]))
              .issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1))
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
              .add_extension(x509.SubjectAlternativeName([x509.DNSName("orders.fixture"), x509.DNSName("localhost")]), False)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False).sign(ca_key, hashes.SHA256()))
    return {"ca.pem": ca.public_bytes(serialization.Encoding.PEM).decode(),
            "server.pem": server.public_bytes(serialization.Encoding.PEM).decode(),
            "server-key.pem": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()).decode()}


class HttpTraffic:
    observer_pod = "observer-denied"

    def __init__(self, env, journey):
        self.env, self.h = env, journey
        self.directory = env.directory / "http"
        self.directory.mkdir(mode=0o700)
        self.config_dir, self.results = self.directory / "config", self.directory / "results"
        self.config_dir.mkdir(mode=0o755)
        self.config_dir.chmod(0o755)
        self.results.mkdir(mode=0o777)
        self.results.chmod(0o777)  # generated fixture trace/journal only
        self.name = env.run + "-http-client"
        self.started = False
        self.run_id, self.token = "traffic-" + uuid4().hex, uuid4().hex
        self.root = Path(__file__).resolve().parents[1]
        self.profile_digest = hashlib.sha256((self.root / "examples/qualification/orders-pod-https-postgres-v1.json").read_bytes()).hexdigest()
        self.scope = {"version": 1, "run_id": self.run_id, "tenant_id": journey.tenant, "profile_digest": self.profile_digest}

    def __enter__(self):
        env, h = self.env, self.h
        certs = web_certificates()
        sources = {p: (self.root / "tests" / p).read_text() for p in ("qualification_order_pod.py", "qualification_orders.py")}
        env.apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "order-code", "namespace": NAMESPACE},
                   "data": sources})
        base = conninfo_to_dict(h.business["dsn"])
        tls_files = {"db-ca.pem": Path(base["sslrootcert"]).read_text(), "db-client.pem": Path(base["sslcert"]).read_text(),
                     "db-client-key.pem": Path(base["sslkey"]).read_text()}
        for side, key in (("source", "src"), ("target", "tgt")):
            dsn = make_conninfo(**{**base, "host": "localhost", "hostaddr": env.database_ip, "port": "5432",
                "user": h.business["app"], "dbname": h.business["names"][key], "sslrootcert": "/etc/order-fixture/db-ca.pem",
                "sslcert": "/etc/order-fixture/db-client.pem", "sslkey": "/etc/order-fixture/db-client-key.pem"})
            env.apply(secret("orders-" + side, {**certs, **tls_files, "dsn": dsn, "token": self.token,
                "config.json": json.dumps({"database": h.business["names"][key], "role": h.business["app"],
                                           "side": side, "scope": self.scope})}))
        service = env.object("service", "orders")
        self.service_uid = service["metadata"]["uid"]
        # The same Service, clusterIP, NodePort and client origin survive the
        # selector change. Only the registered worker changes that selector.
        env.kube("-n", NAMESPACE, "patch", "service", "orders", "--type=merge", "-p", json.dumps({"spec": {
            "type": "NodePort", "ports": [{"port": 8443, "targetPort": 8443, "nodePort": 30443}]}}))
        network = os.environ["SOVA_TEST_QUALIFICATION_DOCKER_NETWORK"]
        node = json.loads(command(["docker", "inspect", env.node]).stdout)[0]
        self.node_ip = node["NetworkSettings"]["Networks"][network]["IPAddress"]
        self.origin = "https://orders.fixture:30443"
        config = {"origin": self.origin, "run_id": self.run_id, "tenant_id": h.tenant, "profile_digest": self.profile_digest}
        for name, value in {"client.json": json.dumps(config), "token": self.token, "ca.pem": certs["ca.pem"],
                            "client.py": (self.root / "tests/qualification_load_client.py").read_text()}.items():
            path = self.config_dir / name
            path.write_text(value)
            path.chmod(0o444)
        env.apply(app_pod(env, self.observer_pod, "observer"))
        env.kube("-n", NAMESPACE, "wait", "--for=condition=Ready", "pod/" + self.observer_pod, "--timeout=60s")
        env.report["http_fixture_sources_sha256"] = {p: hashlib.sha256(v.encode()).hexdigest() for p, v in sources.items()}
        return self

    def pod(self, name, side):
        spec = app_pod(self.env, name, side)
        spec["spec"].update(automountServiceAccountToken=False, securityContext={"runAsNonRoot": True, "runAsUser": 10001,
            "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}})
        container = spec["spec"]["containers"][0]
        container.update(command=["python", "-I", "-B", "/opt/order/qualification_order_pod.py"],
            resources={"limits": {"memory": "192Mi"}},
            readinessProbe={"httpGet": {"path": "/healthz", "port": 8443, "scheme": "HTTPS"}, "periodSeconds": 1},
            volumeMounts=[{"name": "code", "mountPath": "/opt/order", "readOnly": True},
                          {"name": "config", "mountPath": "/etc/order-fixture", "readOnly": True}])
        spec["spec"]["volumes"] = [{"name": "code", "configMap": {"name": "order-code"}},
                                   {"name": "config", "secret": {"secretName": "orders-" + side, "defaultMode": 0o440}}]
        return spec

    def events(self):
        path = self.results / "traffic.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\n")]

    def start(self):
        self.started = True
        command(["docker", "run", "--detach", "--pull", "never", "--name", self.name,
            "--label", "sova.http-qualification=" + self.env.run, "--network", os.environ["SOVA_TEST_QUALIFICATION_DOCKER_NETWORK"],
            "--add-host", "orders.fixture:" + self.node_ip, "--read-only", "--user", "10001:10001",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "128m", "--pids-limit", "32",
            "--mount", f"type=bind,src={self.config_dir},dst=/config,readonly",
            "--mount", f"type=bind,src={self.results},dst=/results", "--entrypoint", "python", self.env.tag,
            "-I", "-B", "/config/client.py"])
        wait_for(lambda: len([e for e in self.events() if e["status"] == 200 and e["side"] == "source"]) >= 3,
                 25, "successful HTTPS orders on the original Service")

    def finish(self):
        wait_for(lambda: len([e for e in self.events() if e["status"] == 200 and e["side"] == "target"]) >= 3,
                 15, "successful HTTPS orders on the same Service after handoff")
        (self.results / "stop").touch()
        exit_code = int(command(["docker", "wait", self.name], timeout=60).stdout)
        assert exit_code == 0, "HTTP client failed; inspect private client log"
        client = json.loads((self.results / "client.json").read_text())
        assert all(client["tls"].values())
        with_journal = RequestJournal(self.results / "client.sqlite")
        try:
            snapshot = with_journal.snapshot()
            observed = observe_orders(with_journal, lambda: self.h.business["connect"](self.h.business["names"]["tgt"]))
            comparison = reconcile(snapshot, observed)
        finally:
            with_journal.close()
        assert comparison["data_integrity"] == "SATISFIED"
        assert comparison["counts"]["confirmed_requests"] == comparison["counts"]["logical_requests"] > 3
        assert comparison["counts"]["unfinished_attempts"] == 0
        assert client["initial_failures"] > 0, "The deliberately interrupted route had no recorded first-attempt failure"
        assert self.env.object("service", "orders")["metadata"]["uid"] == self.service_uid
        (self.results / "independent-observation.json").write_text(json.dumps(observed, indent=2) + "\n")
        (self.results / "reconciliation.json").write_text(json.dumps(comparison, indent=2) + "\n")
        # Failure attempts remain visible even after all logical orders resolve.
        return {"fixed_origin": self.origin, "service_uid": self.service_uid, "tls": client["tls"],
                "client_metrics": client, "reconciliation": comparison, "app_service_account_token_mounted": False}

    def __exit__(self, *exc):
        if self.started:
            found = command(["docker", "inspect", self.name], check=False)
            if found.returncode == 0:
                data = json.loads(found.stdout)[0]
                if data["Config"].get("Labels", {}).get("sova.http-qualification") != self.env.run:
                    raise RuntimeError("Refusing to stop an unowned HTTP client")
                logs = command(["docker", "logs", self.name], check=False)
                (self.directory / "client.log").write_text(logs.stdout + logs.stderr)
                command(["docker", "rm", "--force", self.name])
                self.env.report["http_client_removed"] = True
                self.env.save()
