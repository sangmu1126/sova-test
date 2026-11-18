"""A fresh UUID kind cluster for the real-image worker acceptance scenario.

Never uses the shared test clusters or ~/.kube/config. The caller supplies the
already label-owned PostgreSQL run/network. A host-mounted fixture witness is
shared via a PV: this deliberately does not qualify a production WORM store.
"""

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

from sova.qualification.image import inspect_image

SERVICE_IMAGE = "sha256:09630fd2d03b4221e346cbd63515f2f7d2b5499bf441b7765d7615e1e0dfa4d8"
SERVICE_SOURCE = "c222fde4212c85d4fbb68717d2375ae349a83d80"
NODE_IMAGE = "sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a"
NAMESPACE = "worker-qualification"


def command(args, *, stdin=None, timeout=120, check=True):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        # A caller stores detailed runtime logs in the private run directory.
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr[-800:]}")
    return result


def wait_for(check, seconds, description):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = check()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError("Timed out: " + description)


class PodRun:
    def __init__(self, directory):
        self.directory = directory
        self.run = "sova-pod-" + uuid4().hex[:12]
        self.node = self.run + "-control-plane"
        self.config = directory / "kubeconfig"
        self.namespace = NAMESPACE
        self.tag = "sova-worker-qualification:" + self.run
        self.witness = directory / "witness"
        self.witness.mkdir(mode=0o777)
        self.witness.chmod(0o777)  # synthetic fixture records, never credentials
        self.audit = directory / "audit"
        self.audit.mkdir(mode=0o777)
        self.audit.chmod(0o777)
        self.report = {"run_id": self.run, "shared_cluster_used": False,
                       "service_image_id": SERVICE_IMAGE, "node_image_id": NODE_IMAGE,
                       "service_source_revision": SERVICE_SOURCE,
                       "qualification_issued": False, "phases": []}

    def phase(self, name):
        self.report["phases"].append({"name": name, "at": datetime.now(UTC).isoformat()})
        self.save()
        print("Pod acceptance: " + name, flush=True)

    def save(self):
        (self.directory / "environment.json").write_text(json.dumps(self.report, indent=2) + "\n")

    def kube(self, *args, stdin=None, check=True):
        return command(["kubectl", "--kubeconfig", str(self.config), "--context", "kind-" + self.run,
                        *args], stdin=stdin, check=check).stdout

    def apply(self, *objects):
        self.kube("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))

    def object(self, kind, name):
        return json.loads(self.kube("-n", self.namespace, "get", kind, name, "-o", "json"))

    def exec(self, pod, script):
        return self.kube("-n", self.namespace, "exec", pod, "--", "python", "-I", "-B", "-c", script)

    def audit_events(self):
        path = self.audit / "audit.log"
        if not path.exists():
            return []
        # Ignore only a partially written final line, not malformed complete records.
        lines = path.read_bytes().splitlines(keepends=True)
        return [json.loads(line) for line in lines if line.endswith(b"\n")]


@contextmanager
def owned_cluster(directory):
    directory.mkdir(mode=0o700)
    env = PodRun(directory)
    network = os.environ["SOVA_TEST_QUALIFICATION_DOCKER_NETWORK"]
    database = os.environ["SOVA_TEST_QUALIFICATION_PG_CONTAINER"]
    owner = os.environ["SOVA_TEST_QUALIFICATION_RUN"]
    for kind, name in (("network", network), ("container", database)):
        actual = json.loads(command(["docker", kind, "inspect", name]).stdout)[0]
        labels = actual.get("Labels") if kind == "network" else actual["Config"].get("Labels")
        if (labels or {}).get("sova.qualification") != owner:
            raise RuntimeError("Refusing an unowned database or network")
    inspect_image(SERVICE_IMAGE, "arm64")
    node = json.loads(command(["docker", "image", "inspect", NODE_IMAGE]).stdout)[0]
    if node["Id"] != NODE_IMAGE or node["Architecture"] != "arm64":
        raise RuntimeError("Pinned arm64 node image unavailable")
    # Do not evict or resize other sessions' containers to make room.
    limit = int(command(["docker", "info", "--format", "{{.MemTotal}}"]).stdout)
    stats = command(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"]).stdout.splitlines()
    used = 0
    for usage in stats:
        match = re.fullmatch(r"([0-9.]+)(B|KiB|MiB|GiB)\s*", usage.split("/")[0].strip())
        if not match:
            raise RuntimeError("Cannot determine Docker memory usage")
        used += float(match[1]) * {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[match[2]]
    if limit - used < 1_600_000_000:
        raise RuntimeError("Insufficient spare Docker memory for isolated cluster")
    policy = directory / "audit-policy.json"
    policy.write_text(json.dumps({"apiVersion": "audit.k8s.io/v1", "kind": "Policy", "rules": [
        {"level": "Metadata", "users": [f"system:serviceaccount:{NAMESPACE}:worker",
                                          f"system:serviceaccount:{NAMESPACE}:denied"],
         "verbs": ["get", "patch"], "resources": [{"group": "", "resources": ["pods", "services", "secrets"]}]},
        {"level": "None"}]}))
    patch = """kind: ClusterConfiguration
apiServer:
  extraArgs:
    audit-policy-file: /etc/kubernetes/qualification-audit.json
    audit-log-path: /var/log/qualification/audit.log
    audit-log-maxsize: '10'
    audit-log-maxbackup: '1'
  extraVolumes:
    - name: qualification-audit-policy
      hostPath: /etc/kubernetes/qualification-audit.json
      mountPath: /etc/kubernetes/qualification-audit.json
      readOnly: true
      pathType: File
    - name: qualification-audit-log
      hostPath: /var/log/qualification
      mountPath: /var/log/qualification
      pathType: DirectoryOrCreate
"""
    kind_config = directory / "kind.json"
    kind_config.write_text(json.dumps({"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4",
        "networking": {"apiServerAddress": "127.0.0.1"}, "nodes": [{"role": "control-plane",
        "extraMounts": [
            {"hostPath": str(env.witness.resolve()), "containerPath": "/qualification-witness"},
            {"hostPath": str(policy.resolve()), "containerPath": "/etc/kubernetes/qualification-audit.json", "readOnly": True},
            {"hostPath": str(env.audit.resolve()), "containerPath": "/var/log/qualification"}],
        "kubeadmConfigPatches": [patch]}]}))
    attempted = tagged = False
    try:
        attempted = True
        env.phase("creating owned cluster")
        creation = command(["kind", "create", "cluster", "--name", env.run, "--image", NODE_IMAGE,
                            "--config", str(kind_config), "--kubeconfig", str(env.config),
                            "--wait", "90s"], timeout=240, check=False)
        (directory / "kind-create.log").write_text(creation.stdout + creation.stderr)
        if creation.returncode:
            raise RuntimeError("Owned kind cluster creation failed; see private kind-create.log")
        command(["docker", "update", "--memory", "2048m", "--memory-swap", "2048m", env.node])
        command(["docker", "network", "connect", network, env.node])
        command(["docker", "tag", SERVICE_IMAGE, env.tag])
        tagged = True
        env.phase("loading fixed service image")
        command(["kind", "load", "docker-image", env.tag, "--name", env.run], timeout=180)
        loaded = json.loads(command(["docker", "exec", env.node, "crictl", "inspecti", env.tag]).stdout)
        env.report["loaded_cri_image"] = loaded["status"]
        env.report["cluster_uid"] = json.loads(env.kube("get", "namespace", "kube-system", "-o", "json"))["metadata"]["uid"]
        env.report["version"] = json.loads(env.kube("version", "-o", "json"))["serverVersion"]
        env.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE,
                   "labels": {"pod-security.kubernetes.io/enforce": "restricted"}}})
        # All generated material is inside an explicitly separate local cluster.
        info = json.loads(command(["docker", "inspect", database]).stdout)[0]
        env.database_ip = info["NetworkSettings"]["Networks"][network]["IPAddress"]
        env.report["database_hostaddr"] = env.database_ip
        env.phase("owned cluster ready")
        yield env
    finally:
        if attempted:
            found = command(["docker", "inspect", env.node], check=False)
            if found.returncode == 0:
                node = json.loads(found.stdout)[0]
                if node["Config"].get("Labels", {}).get("io.x-k8s.kind.cluster") != env.run:
                    raise RuntimeError("Refusing to delete an unowned node")
                try:
                    if env.config.exists():
                        for args, filename in ((["-n", NAMESPACE, "get", "pods", "-o", "json"], "pods-final.json"),
                                               (["-n", NAMESPACE, "get", "events", "-o", "json"], "events-final.json")):
                            (directory / filename).write_text(env.kube(*args, check=False))
                        pods = json.loads(env.kube("-n", NAMESPACE, "get", "pods", "-o", "json", check=False) or '{"items":[]}')
                        for pod in pods.get("items", []):
                            name = pod["metadata"]["name"]
                            (directory / (name + ".log")).write_text(env.kube("-n", NAMESPACE, "logs", name, check=False))
                except (RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    env.report["diagnostic_collection_error"] = type(error).__name__
                finally:
                    command(["kind", "delete", "cluster", "--name", env.run], timeout=120)
            env.report["cluster_removed"] = command(["docker", "inspect", env.node], check=False).returncode != 0
        if tagged:
            command(["docker", "image", "rm", env.tag])
            env.report["temporary_image_tag_removed"] = True
        env.save()


def secret(name, values):
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": NAMESPACE},
            "type": "Opaque", "stringData": values}


def worker_pod(env, name):
    values = {
        "SOVA_DATABASE_URL_FILE": "/etc/sova/runtime-dsn", "SOVA_TENANTS": env.tenant,
        "SOVA_OIDC_ISSUER": "https://fixture.invalid", "SOVA_OIDC_AUDIENCE": "fixture",
        "SOVA_OIDC_JWKS_URL": "https://fixture.invalid/keys",
        "SOVA_REGISTRY_VERIFIER_VERSION": "registry-attestor-1", "SOVA_EVIDENCE_VERIFIER": "handoff-verifier-1",
        "SOVA_EVIDENCE_ISSUER": "handoff-producer", "SOVA_EVIDENCE_KEY_FILE": "/etc/sova/key",
        "SOVA_EVIDENCE_KEY_ID": "k1", "SOVA_EXECUTION_FILE": "/etc/sova/execution.json",
        "SOVA_WITNESS": "file:/witness", "SOVA_LEASE_SECONDS": "10", "SOVA_WORKER_STALL_SECONDS": "180",
    }
    downward = [{"name": k, "valueFrom": {"fieldRef": {"fieldPath": v}}} for k, v in (
        ("POD_NAME", "metadata.name"), ("POD_UID", "metadata.uid"), ("POD_NAMESPACE", "metadata.namespace"))]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NAMESPACE}, "spec": {
        "serviceAccountName": "worker", "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "worker", "image": env.tag, "imagePullPolicy": "Never",
            "args": ["run-worker", "--poll-seconds", "0.5"],
            "env": [{"name": k, "value": v} for k, v in values.items()] + downward,
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8081}, "periodSeconds": 1},
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            "volumeMounts": [{"name": "config", "mountPath": "/etc/sova", "readOnly": True},
                             {"name": "witness", "mountPath": "/witness"}]}],
        "volumes": [{"name": "config", "secret": {"secretName": "worker-config", "defaultMode": 0o440}},
                    {"name": "witness", "persistentVolumeClaim": {"claimName": "witness"}}]}}


def prepare_workload(env, journey, dsn, runtime_role):
    from datetime import timedelta
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from postgres_business import PROBE_INSERT, SCHEMA, TABLES

    env.tenant = journey.tenant
    namespace = NAMESPACE
    ca = env.object("configmap", "kube-root-ca.crt")["data"]["ca.crt"]
    now = datetime.now(UTC)
    names = journey.business["names"]
    sides = {names["src"]: {"app": "orders", "side": "source"}, names["tgt"]: {"app": "orders", "side": "target"}}
    registration = {"schema_version": 1, "tenant_id": journey.tenant, "application_id": journey.app,
        "environment_id": "kind-local", "location": "local", "endpoint": "https://kubernetes.default.svc",
        "approved_addresses": [json.loads(env.kube("-n", "default", "get", "service", "kubernetes", "-o", "json"))["spec"]["clusterIP"]],
        "ca_digest": hashlib.sha256(ca.encode()).hexdigest(), "namespace": namespace,
        "namespace_uid": env.object("namespace", namespace)["metadata"]["uid"], "revision": 1,
        "registered_at": (now - timedelta(seconds=1)).isoformat(), "valid_until": (now + timedelta(minutes=10)).isoformat()}
    params = conninfo_to_dict(dsn)
    files = {}
    for setting in ("sslrootcert", "sslcert", "sslkey"):
        files[setting] = Path(params[setting]).read_text()
        params[setting] = "/etc/sova/" + setting
    # libpq authenticates the generated certificate's localhost name while
    # connecting to the exact IP read from this run-owned Docker network.
    params.update(host="localhost", hostaddr=env.database_ip, port="5432")
    execution = {"version": 1, "applications": [{"tenant_id": journey.tenant, "application_id": journey.app,
        "adapter_id": journey.world["manifest"].adapter_id, "mode": "offline_copy",
        "databases": {name: {"dsn_file": "/etc/sova/business-dsn"} for name in names.values()},
        "source_db": names["src"], "target_db": names["tgt"], "scratch_db": names["scratch"], "other_db": names["other"],
        "app_role": journey.business["app"], "writer_roles": [journey.business["app"], journey.business["batch"]],
        "tables": [list(t) for t in TABLES], "schema_sql_file": "/etc/sova/schema.sql", "probe_insert": PROBE_INSERT,
        "route": {"kind": "kubernetes_service", "registration_file": "/etc/sova/route.json", "service": "orders",
                  "sides": sides, "converge_seconds": 120}}]}
    files.update({"runtime-dsn": make_conninfo(**{**params, "user": runtime_role}),
                  "business-dsn": make_conninfo(**params), "execution.json": json.dumps(execution),
                  "schema.sql": SCHEMA, "route.json": json.dumps(registration),
                  "key": journey.signer._key.private_bytes_raw().hex()})
    env.apply(
        {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "witness"}, "spec": {
            "capacity": {"storage": "16Mi"}, "accessModes": ["ReadWriteOnce"], "storageClassName": "",
            "hostPath": {"path": "/qualification-witness"}, "persistentVolumeReclaimPolicy": "Retain"}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "witness", "namespace": namespace},
         "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "", "volumeName": "witness",
                  "resources": {"requests": {"storage": "16Mi"}}}},
        *[{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name, "namespace": namespace}}
          for name in ("worker", "denied")],
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "worker", "namespace": namespace},
         "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                   {"apiGroups": [""], "resources": ["services"], "resourceNames": ["orders"], "verbs": ["get", "patch"]},
                   {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["list"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "worker", "namespace": namespace},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "worker"},
         "subjects": [{"kind": "ServiceAccount", "name": "worker", "namespace": namespace}]},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "orders", "namespace": namespace},
         "spec": {"selector": sides[names["src"]], "ports": [{"port": 80}]}}, secret("worker-config", files))
    return sides


def app_pod(env, name, side, *, service_account="denied"):
    # Endpoint fixtures; these do not claim to serve a real HTTP order app.
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NAMESPACE,
            "labels": {"app": "orders", "side": side}}, "spec": {"serviceAccountName": service_account,
        "restartPolicy": "Never", "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
             "seccompProfile": {"type": "RuntimeDefault"}}, "containers": [{"name": "endpoint", "image": env.tag,
             "imagePullPolicy": "Never", "command": ["python", "-I", "-B", "-c", "import time; time.sleep(3600)"],
             "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                 "capabilities": {"drop": ["ALL"]}},
             "resources": {"limits": {"memory": "64Mi"}}}]}}
