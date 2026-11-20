"""Replace only expired PKI/config Secrets on the owned, idle target.

Existing database/PVC, image, private keys, passwords and schema are preserved.
The immutable new Secrets are bound by UID/RV/old-reference guarded patches.
"""

import argparse
import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess

from import_target_images import CONFIG, KUBECTL, VM, guest, identity


NS = "sj-b0be270e62"
RUN = "b0be270e62"


def mutate(document, *arguments):
    command = ["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n", KUBECTL, "--kubeconfig", CONFIG, *arguments]
    result = subprocess.run(command, input=json.dumps(document).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError("Target mutation refused; inspect owned resources before retry")
    return result.stdout.decode().strip()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--previous-generation", type=int, default=2)
    p.add_argument("--generation", type=int, default=3)
    args = p.parse_args()
    if not 1 <= args.previous_generation < args.generation <= 32 or args.generation != args.previous_generation + 1:
        raise ValueError("One explicit consecutive owned credential generation required")
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity()
    ns = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "get", "ns", NS, "-o", "json"))
    if ns["metadata"]["uid"] != "565b91c4-4f6e-4d06-94c6-9f8803afa6c9":
        raise ValueError("Destination namespace replaced")
    bindings = json.loads((args.inputs / "identity.json").read_text())
    if bindings["run_id"] != RUN or datetime.now(UTC) >= datetime.fromisoformat(bindings["expires_at"]):
        raise ValueError("Unexpired owned new window required")
    counts = guest(KUBECTL, "--kubeconfig", CONFIG, "-n", NS, "exec", "deployment/orders-db", "--",
        "psql", "-U", "postgres", "-d", "sj_control_b0be270e62", "-tAc",
        "SELECT (SELECT count(*) FROM sova_control.plans),(SELECT count(*) FROM sova_control.operations),(SELECT count(*) FROM sova_control.effects)").decode().strip()
    if counts != "0|0|0":
        raise ValueError("Existing plans or effects require coordinated credential rotation")
    report = {"run_id": RUN, "expires_at": bindings["expires_at"], "plan_operation_effect_counts": counts,
              "changed": [], "business_data_or_image_changed": False, "migration_complete": False}
    for kind in ("db", "app"):
        deployment = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "-n", NS, "get", "deployment", "orders-" + kind, "-o", "json"))
        if deployment["metadata"]["labels"].get("sova.run") != RUN:
            raise ValueError("Workload ownership changed")
        volumes = deployment["spec"]["template"]["spec"]["volumes"]
        index = next(i for i, volume in enumerate(volumes) if volume["name"] == "input")
        old_name = volumes[index]["secret"]["secretName"]
        if old_name != "orders-" + kind + "-input-v" + str(args.previous_generation):
            raise ValueError("Expected previous input reference changed; do not overwrite")
        old = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "-n", NS, "get", "secret", old_name, "-o", "json"))
        directory = args.inputs / ("target-" + kind)
        data = {path.name: base64.b64encode(path.read_bytes()).decode() for path in directory.iterdir()}
        if set(data) != set(old["data"]):
            raise ValueError("Mounted input inventory changed")
        allowed = {"ca.pem", "server.pem"} | ({"database-ca.pem", "config.json"} if kind == "app" else set())
        for name, value in data.items():
            if name not in allowed and value != old["data"][name]:
                raise ValueError("Only certificate/config expiry may change")
            if name == "config.json":
                previous, current = [json.loads(base64.b64decode(raw)) for raw in (old["data"][name], value)]
                previous.pop("expires_at")
                if current.pop("expires_at") != bindings["expires_at"] or previous != current:
                    raise ValueError("App scope or immutable deployment changed")
        new_name = "orders-" + kind + "-input-v" + str(args.generation)
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": new_name, "namespace": NS,
            "labels": {"sova.run": RUN, "sova.scope": "personal-qualification"}}, "immutable": True, "type": "Opaque", "data": data}
        mutate(secret, "create", "--dry-run=server", "-f", "-", "-o", "name")
        mutate(secret, "create", "-f", "-", "-o", "name")
        path = f"/spec/template/spec/volumes/{index}/secret/secretName"
        patch = [{"op": "test", "path": "/metadata/uid", "value": deployment["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
            {"op": "test", "path": path, "value": old_name}, {"op": "replace", "path": path, "value": new_name}]
        # kubectl JSON patch stdin avoids placing secrets or patches in argv.
        command = ["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n", KUBECTL, "--kubeconfig", CONFIG,
                   "-n", NS, "patch", "deployment", "orders-" + kind, "--type=json", "--patch-file=/dev/stdin", "-o", "name"]
        result = subprocess.run(command, input=json.dumps(patch).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError("Guarded rollout patch refused; new immutable Secret retained")
        guest(KUBECTL, "--kubeconfig", CONFIG, "-n", NS, "rollout", "status", "deployment/orders-" + kind, "--timeout=90s")
        report["changed"].append({"deployment": "orders-" + kind, "uid": deployment["metadata"]["uid"], "old_secret": old_name, "new_secret": new_name})
        (args.artifacts / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"rollouts": len(report["changed"]), "business_data_or_image_changed": False, "migration_complete": False}))


if __name__ == "__main__":
    main()
