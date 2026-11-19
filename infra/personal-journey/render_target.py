"""Render new run-owned Kubernetes resources into a PRIVATE JSON file.

Contains Secret values. Never print the rendered document, commit it or use
it against a shared/team cluster. Apply only after checking VM/cluster identity,
the active encrypted volume and exact cached OCI content independently.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import re


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--volume-proof", type=Path, required=True)
    p.add_argument("--image-proof", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    identity = json.loads((args.inputs / "identity.json").read_text())
    volume = json.loads(args.volume_proof.read_text())
    images = json.loads(args.image_proof.read_text())
    run, ns = identity["run_id"], identity["namespace"]
    if (not re.fullmatch("[a-z0-9]{8,12}", run) or ns != "sj-" + run
            or volume["run_id"] != run or images["run_id"] != run
            or images["cluster_uid"] != volume["cluster_uid"]
            or volume["cluster_uid"] != "9de35591-57a3-4759-bd73-6340e2de0b61"
            or volume["type"] != "LUKS2" or volume["cipher"] != "aes-xts-plain64"
            or volume["key_written_on_vm"] is not False or volume["wrong_key_rejected"] is not True
            or images["images"]["app"]["manifest_digest"] != identity["app_manifest"]
            or images["images"]["db"]["manifest_digest"] != identity["postgres_manifest"]):
        raise ValueError("Actual target proofs do not bind these deployment inputs")
    labels = {"sova.run": run, "sova.scope": "personal-qualification"}
    resources = []

    def obj(kind, name, *, api="v1", cluster=False, **values):
        value = {"apiVersion": api, "kind": kind,
                 "metadata": {"name": name, "labels": labels.copy(), **({} if cluster else {"namespace": ns})},
                 **values}
        resources.append(value)
        return value

    namespace = obj("Namespace", ns, cluster=True)
    namespace["metadata"]["labels"].update({"pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.35", "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/warn": "restricted"})
    obj("ResourceQuota", "run-boundary", spec={"hard": {"pods": "8", "requests.cpu": "2",
        "requests.memory": "1536Mi", "limits.cpu": "4", "limits.memory": "2Gi",
        "persistentvolumeclaims": "1", "requests.storage": "1Gi"}})
    for role in ("app", "db", "route-worker"):
        obj("ServiceAccount", "orders-" + role, automountServiceAccountToken=False)
    storage = ns + "-encrypted"
    obj("StorageClass", storage, api="storage.k8s.io/v1", cluster=True,
        provisioner="kubernetes.io/no-provisioner", reclaimPolicy="Retain", volumeBindingMode="WaitForFirstConsumer")
    obj("PersistentVolume", storage, cluster=True, spec={"capacity": {"storage": "900Mi"},
        "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem", "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": storage, "claimRef": {"namespace": ns, "name": "orders-data"},
        "local": {"path": volume["mount"]}, "nodeAffinity": {"required": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["sova-rke2-target"]}]}]}}})
    obj("PersistentVolumeClaim", "orders-data", spec={"storageClassName": storage,
        "volumeName": storage, "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "900Mi"}}})
    for role in ("app", "db"):
        data = {file.name: base64.b64encode(file.read_bytes()).decode()
                for file in sorted((args.inputs / ("target-" + role)).iterdir())}
        obj("Secret", "orders-" + role + "-input", immutable=True, type="Opaque", data=data)
    obj("NetworkPolicy", "default-deny", api="networking.k8s.io/v1",
        spec={"podSelector": {}, "policyTypes": ["Ingress", "Egress"]})
    dns = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}], "ports": [
            {"protocol": protocol, "port": 53} for protocol in ("TCP", "UDP")]}
    obj("NetworkPolicy", "app-database-only", api="networking.k8s.io/v1", spec={
        "podSelector": {"matchLabels": {"sova.role": "app"}}, "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": {"sova.role": role}}}
            for role in ("edge", "qualifier")], "ports": [{"protocol": "TCP", "port": 8080}]}],
        "egress": [dns, {"to": [{"podSelector": {"matchLabels": {"sova.role": "db"}}}],
            "ports": [{"protocol": "TCP", "port": 5432}]}]})
    obj("NetworkPolicy", "database-registered-clients", api="networking.k8s.io/v1", spec={
        "podSelector": {"matchLabels": {"sova.role": "db"}}, "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": {"sova.role": role}}}
            for role in ("app", "qualifier", "worker")], "ports": [{"protocol": "TCP", "port": 5432}]}], "egress": []})
    obj("Role", "orders-route", api="rbac.authorization.k8s.io/v1", rules=[
        {"apiGroups": [""], "resources": ["services"], "resourceNames": ["orders-entry"], "verbs": ["get", "patch"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
        {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["get", "list"]}])
    obj("RoleBinding", "orders-route", api="rbac.authorization.k8s.io/v1",
        roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "orders-route"},
        subjects=[{"kind": "ServiceAccount", "name": "orders-route-worker", "namespace": ns}])

    def deployment(role, uid, container, volumes):
        selector = {"sova.run": run, "sova.role": role}
        obj("Deployment", "orders-" + role, api="apps/v1", spec={"replicas": 1,
            "strategy": {"type": "Recreate"}, "selector": {"matchLabels": selector}, "template": {
                "metadata": {"labels": {**labels, **selector}}, "spec": {
                    "serviceAccountName": "orders-" + role, "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid,
                        "fsGroup": uid, "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{**container, "securityContext": {"allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}}],
                    "volumes": volumes}}})
    def secret(role):
        return {"name": "input", "secret": {"secretName": "orders-" + role + "-input", "defaultMode": 0o440}}
    deployment("db", 70, {"name": "postgres", "image": images["images"]["db"]["reference"], "imagePullPolicy": "Never",
        "command": ["sh", "-ec", 'test "$(cat /var/lib/postgresql/data/.sova-encrypted-volume)" = ' + run
            + '; test -d /var/lib/postgresql/data/pgdata; exec docker-entrypoint.sh "$@"', "qualification"],
        "args": ["postgres", "-c", "ssl=on", "-c", "ssl_cert_file=/run/sova-db/server.pem",
            "-c", "ssl_key_file=/run/sova-db/server-key.pem", "-c", "hba_file=/run/sova-db/pg_hba.conf",
            "-c", "password_encryption=scram-sha-256", "-c", "max_connections=40", "-c", "shared_buffers=64MB",
            "-c", "log_min_error_statement=panic"],
        "env": [{"name": name, "value": value} for name, value in {
            "POSTGRES_DB": "target_orders", "POSTGRES_PASSWORD_FILE": "/run/sova-db/admin-password",
            "PGDATA": "/var/lib/postgresql/data/pgdata",
            "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256 --auth-local=peer --data-checksums"}.items()],
        "ports": [{"containerPort": 5432}], "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"}},
        "readinessProbe": {"exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "target_orders"]},
            "periodSeconds": 3, "timeoutSeconds": 2},
        "volumeMounts": [{"name": "input", "mountPath": "/run/sova-db", "readOnly": True},
            {"name": "input", "mountPath": "/docker-entrypoint-initdb.d/001-run.sql", "subPath": "init.sql", "readOnly": True},
            {"name": "data", "mountPath": "/var/lib/postgresql/data"},
            {"name": "socket", "mountPath": "/var/run/postgresql"}, {"name": "tmp", "mountPath": "/tmp"}]},
        [secret("db"), {"name": "data", "persistentVolumeClaim": {"claimName": "orders-data"}},
            {"name": "socket", "emptyDir": {"medium": "Memory", "sizeLimit": "2Mi"}},
            {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "4Mi"}}])
    deployment("app", 10001, {"name": "orders", "image": images["images"]["app"]["reference"], "imagePullPolicy": "Never",
        "args": ["python", "-m", "uvicorn", "sova.qualification.order_runtime:create_app", "--factory",
            "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers",
            "--limit-concurrency", "24", "--backlog", "64", "--timeout-keep-alive", "3", "--no-server-header",
            "--ssl-certfile", "/run/sova/server.pem", "--ssl-keyfile", "/run/sova/server-key.pem"],
        "ports": [{"containerPort": 8080}], "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
            "limits": {"cpu": "1", "memory": "256Mi"}},
        # Kubelet's HTTPS probe is process liveness only, NOT CA/identity proof.
        "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080, "scheme": "HTTPS"}, "periodSeconds": 3},
        "volumeMounts": [{"name": "input", "mountPath": "/run/sova", "readOnly": True}]}, [secret("app")])
    for role, port in (("db", 5432), ("app", 8080)):
        obj("Service", "orders-" + role, spec={"selector": {"sova.run": run, "sova.role": role},
            "ports": [{"name": "tls", "port": port, "targetPort": port}]})
    with args.output.open("x") as file:
        json.dump({"apiVersion": "v1", "kind": "List", "items": resources}, file)
    print(json.dumps({"run_id": run, "resources_rendered": len(resources), "deployed": False,
        "secret_values_printed": False, "no_existing_resources_reused": True}))


if __name__ == "__main__":
    main()
