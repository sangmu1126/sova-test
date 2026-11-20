"""Renew owned HAProxy TLS/backends without changing the entry Service selector."""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path

from import_target_images import CONFIG, KUBECTL, guest, identity
from renew_target import mutate


NS = "sj-b0be270e62"


def get(kind, name):
    return json.loads(
        guest(
            KUBECTL, "--kubeconfig", CONFIG, "-n", NS, "get", kind, name, "-o", "json"
        )
    )


def patch(kind, name, resource, operations):
    guard = [
        {"op": "test", "path": "/metadata/uid", "value": resource["metadata"]["uid"]},
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": resource["metadata"]["resourceVersion"],
        },
    ]
    return mutate(
        [*guard, *operations],
        "-n",
        NS,
        "patch",
        kind,
        name,
        "--type=json",
        "--patch-file=/dev/stdin",
        "-o",
        "name",
    )


def renew_tls_only(args, service, wanted):
    if (
        not 1 <= args.previous_generation < args.generation <= 32
        or args.generation != args.previous_generation + 1
    ):
        raise ValueError("Explicit consecutive TLS input generations required")
    prepared = json.loads((args.prepared / "entry.json").read_text())
    if (
        prepared["run_id"] != "b0be270e62"
        or prepared["namespace"] != NS
        or datetime.now(UTC) >= datetime.fromisoformat(prepared["expires_at"])
    ):
        raise ValueError("Fresh owned entry credentials required")
    objects = json.loads((args.prepared / "resources.json").read_text())["items"]
    candidates = [o for o in objects if o["kind"] == "Secret"]
    if len(candidates) != 1:
        raise ValueError("Only the one prepared TLS Secret is accepted")
    secret = candidates[0]
    if (
        secret["metadata"]["name"] != "orders-entry-tls"
        or secret["metadata"]["namespace"] != NS
        or secret["metadata"]["labels"].get("sova.run") != "b0be270e62"
        or secret.get("immutable") is not True
        or set(secret["data"]) != {"entry.pem", "ca.pem"}
    ):
        raise ValueError("Exact prepared entry TLS Secret required")
    old_name = "orders-entry-tls-v" + str(args.previous_generation)
    new_name = "orders-entry-tls-v" + str(args.generation)
    deployments = {}
    for side in ("source", "target"):
        name = "orders-entry-" + side
        current = get("deployment", name)
        volumes = current["spec"]["template"]["spec"]["volumes"]
        index = next(i for i, v in enumerate(volumes) if v["name"] == "tls")
        if (
            current["metadata"]["labels"].get("sova.run") != "b0be270e62"
            or volumes[index]["secret"]["secretName"] != old_name
        ):
            raise ValueError("Previous owned TLS input changed before renewal")
        deployments[name] = (current, index)
    policy = get("networkpolicy", "entry-registered-upstreams")
    secret["metadata"]["name"] = new_name
    report = {
        "tls_only": True,
        "previous_secret": old_name,
        "new_secret": new_name,
        "certificate_sha256": prepared["certificate_sha256"],
        "expires_at": prepared["expires_at"],
        "patches": [],
        "route_config_or_image_changed": False,
        "service_selector_changed": False,
    }
    output = args.artifacts / "result.json"
    output.write_text(json.dumps(report, indent=2))
    mutate(secret, "create", "--dry-run=server", "-f", "-", "-o", "name")
    mutate(secret, "create", "-f", "-", "-o", "name")
    for name, (current, index) in deployments.items():
        item = {
            "deployment": name,
            "uid": current["metadata"]["uid"],
            "patch_requested": True,
            "rollout_complete": False,
        }
        report["patches"].append(item)
        output.write_text(json.dumps(report, indent=2))
        path = f"/spec/template/spec/volumes/{index}/secret/secretName"
        patch(
            "deployment",
            name,
            current,
            [
                {"op": "test", "path": path, "value": old_name},
                {"op": "replace", "path": path, "value": new_name},
            ],
        )
        item["patch_api_success"] = True
        output.write_text(json.dumps(report, indent=2))
        guest(
            KUBECTL,
            "--kubeconfig",
            CONFIG,
            "-n",
            NS,
            "rollout",
            "status",
            "deployment/" + name,
            "--timeout=90s",
        )
        after = get("deployment", name)
        old_spec, new_spec = (
            current["spec"]["template"]["spec"],
            after["spec"]["template"]["spec"],
        )
        if (
            after["metadata"]["uid"] != current["metadata"]["uid"]
            or old_spec["containers"] != new_spec["containers"]
            or [v for v in old_spec["volumes"] if v["name"] != "tls"]
            != [v for v in new_spec["volumes"] if v["name"] != "tls"]
        ):
            raise ValueError("A non-TLS entry input changed during renewal")
        item["rollout_complete"] = True
        output.write_text(json.dumps(report, indent=2))
    after = get("service", "orders-entry")
    if (after["metadata"]["uid"], after["spec"]["selector"]) != (
        service["metadata"]["uid"],
        wanted,
    ) or get("networkpolicy", "entry-registered-upstreams")["spec"] != policy["spec"]:
        raise ValueError("Actual routing or policy changed during certificate renewal")
    print(json.dumps(report))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--tls-only", action="store_true")
    p.add_argument("--previous-generation", type=int)
    p.add_argument("--generation", type=int)
    args = p.parse_args()
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity()
    service = get("service", "orders-entry")
    wanted = {"sova.run": "b0be270e62", "sova.role": "edge", "sova.side": "source"}
    if (
        service["metadata"]["uid"] != "c5565701-1035-4140-aaf1-e6442a89b2d9"
        or service["spec"]["selector"] != wanted
    ):
        raise ValueError("Entry has changed or a migration has started")
    if args.tls_only:
        if args.previous_generation is None or args.generation is None:
            raise ValueError("TLS-only renewal requires explicit old/new generations")
        return renew_tls_only(args, service, wanted)
    objects = json.loads((args.prepared / "resources.json").read_text())["items"]
    report = {
        "created": [],
        "patched": [],
        "service_selector_changed": False,
        "business_data_changed": False,
    }
    for item in objects:
        if item["kind"] not in ("Secret", "ConfigMap"):
            continue
        item["metadata"]["name"] += "-v3"
        if (
            item["metadata"]["namespace"] != NS
            or item["metadata"]["labels"].get("sova.run") != "b0be270e62"
        ):
            raise ValueError("Prepared entry ownership mismatch")
        mutate(item, "create", "--dry-run=server", "-f", "-", "-o", "name")
        report["created"].append(mutate(item, "create", "-f", "-", "-o", "name"))
        (args.artifacts / "result.json").write_text(json.dumps(report, indent=2))
    policy = get("networkpolicy", "entry-registered-upstreams")
    new_policy = next(item for item in objects if item["kind"] == "NetworkPolicy")
    old_port = policy["spec"]["egress"][0]["ports"][0]["port"]
    if old_port != 64900 or policy["spec"]["egress"][0]["to"] != [
        {"ipBlock": {"cidr": "192.168.5.2/32"}}
    ]:
        raise ValueError("Earlier source egress policy changed")
    patch(
        "networkpolicy",
        "entry-registered-upstreams",
        policy,
        [
            {"op": "test", "path": "/spec/egress/0/ports/0/port", "value": old_port},
            {
                "op": "replace",
                "path": "/spec/egress/0/ports/0/port",
                "value": new_policy["spec"]["egress"][0]["ports"][0]["port"],
            },
        ],
    )
    for side in ("source", "target"):
        name = "orders-entry-" + side
        current = get("deployment", name)
        if current["metadata"]["labels"].get("sova.run") != "b0be270e62":
            raise ValueError("Entry deployment ownership changed")
        volumes = current["spec"]["template"]["spec"]["volumes"]
        changes = []
        for volume, field, old_name in (
            ("config", "configMap", name),
            ("tls", "secret", "orders-entry-tls"),
        ):
            index = next(
                i for i, entry in enumerate(volumes) if entry["name"] == volume
            )
            key = "name" if field == "configMap" else "secretName"
            path = f"/spec/template/spec/volumes/{index}/{field}/{key}"
            if volumes[index][field][key] != old_name:
                raise ValueError("Entry input revision changed")
            changes += [
                {"op": "test", "path": path, "value": old_name},
                {"op": "replace", "path": path, "value": old_name + "-v3"},
            ]
        patch("deployment", name, current, changes)
        guest(
            KUBECTL,
            "--kubeconfig",
            CONFIG,
            "-n",
            NS,
            "rollout",
            "status",
            "deployment/" + name,
            "--timeout=90s",
        )
        report["patched"].append(name)
        (args.artifacts / "result.json").write_text(json.dumps(report, indent=2))
    after = get("service", "orders-entry")
    if (
        after["metadata"]["uid"] != service["metadata"]["uid"]
        or after["spec"]["selector"] != wanted
    ):
        raise ValueError("Entry Service changed during renewal")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
