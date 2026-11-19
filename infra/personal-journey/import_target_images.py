"""Import the exact inspected OCI artifacts into the verified personal VM.

Only image-cache additions. No namespace, workload, policy or route changes.
No host filesystem sharing, registry credentials or plaintext secret transfer.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from prepare_canary import artifact


VM = "sova-rke2-target"
CTR = "/var/lib/rancher/rke2/bin/ctr"
SOCKET = "/run/k3s/containerd/containerd.sock"
KUBECTL = "/var/lib/rancher/rke2/bin/kubectl"
CONFIG = "/etc/rancher/rke2/rke2.yaml"
CLUSTER = "9de35591-57a3-4759-bd73-6340e2de0b61"


def guest(*args):
    return subprocess.check_output(["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n", *args],
                                   stderr=subprocess.PIPE, timeout=60)


def identity():
    if (guest("cat", "/etc/sova-qualification-id").decode().strip() != "sova-rke2-qualification-v1"
            or guest("cat", "/etc/machine-id").decode().strip() != "a789f623bca040989f282881d9a21654"
            or guest("uname", "-m").decode().strip() != "aarch64"
            or guest(KUBECTL, "--kubeconfig", CONFIG, "get", "ns", "kube-system", "-o",
                     "jsonpath={.metadata.uid}").decode() != CLUSTER):
        raise ValueError("Wrong physical destination")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--app-image-directory", required=True, type=Path)
    p.add_argument("--postgres-image-directory", required=True, type=Path)
    p.add_argument("--artifacts", required=True, type=Path)
    p.add_argument("--proxy-image-directory", type=Path)
    p.add_argument("--existing-namespace-uid")
    args = p.parse_args()
    if not re.fullmatch("[a-z0-9]{8,12}", args.run_id):
        raise ValueError("Fresh run ID required")
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity()
    found = guest(KUBECTL, "--kubeconfig", CONFIG, "get", "ns", "sj-" + args.run_id,
                  "--ignore-not-found", "-o", "json")
    if found.strip():
        existing = json.loads(found)["metadata"]
        if (existing["uid"] != args.existing_namespace_uid
                or existing.get("labels", {}).get("sova.run") != args.run_id):
            raise ValueError("Namespace exists without an explicit matching UID/owner; refuse reuse")
    elif args.existing_namespace_uid:
        raise ValueError("Expected existing namespace disappeared")
    images = {}
    inputs = [("app", args.app_image_directory), ("db", args.postgres_image_directory)]
    if args.proxy_image_directory:
        inputs.append(("proxy", args.proxy_image_directory))
    for kind, directory in inputs:
        proof = artifact(directory)
        remote = "/tmp/sova-" + args.run_id + "-" + kind + ".oci.tar"
        subprocess.run(["limactl", "copy", str(directory / "canary.oci.tar"), VM + ":" + remote],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if guest("sha256sum", remote).decode().split()[0] != proof["archive_sha256"]:
            raise ValueError("Transferred OCI archive differs")
        identity()
        base = "local.sova.invalid/" + args.run_id + "/" + kind
        output = guest(CTR, "--address", SOCKET, "--namespace", "k8s.io", "images", "import",
                       "--base-name", base, "--digests", remote)
        (args.artifacts / (kind + "-import.log")).write_bytes(output)
        for key in ("manifest_digest", "config_digest"):
            blob = guest(CTR, "--address", SOCKET, "--namespace", "k8s.io", "content", "get", proof[key])
            if "sha256:" + hashlib.sha256(blob).hexdigest() != proof[key]:
                raise ValueError("Runtime content differs from inspected OCI bytes")
        reference = base + "@" + proof["manifest_digest"]
        refs = guest(CTR, "--address", SOCKET, "--namespace", "k8s.io", "images", "list", "-q").decode().splitlines()
        if reference not in refs:
            raise ValueError("Exact digest reference not imported; do not fall back to a tag")
        images[kind] = {"reference": reference, "manifest_digest": proof["manifest_digest"],
                        "config_digest": proof["config_digest"], "archive_sha256": proof["archive_sha256"]}
    result = {"run_id": args.run_id, "cluster_uid": CLUSTER, "images": images,
              "workload_deployment_performed": False, "operational_qualification": False}
    (args.artifacts / "target-images.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"run_id": args.run_id, "exact_images_imported": len(images), "workload_deployed": False}))


if __name__ == "__main__":
    main()
