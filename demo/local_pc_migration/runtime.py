"""Identity-bound operator tooling. No credentials, manifests or targets from browsers."""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess


def command(args, *, data=None, timeout=120, binary=False):
    result = subprocess.run(
        args, input=data, capture_output=True, text=not binary, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError("registered_operation_failed")
    return result.stdout


def atomic(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False))
    temp.chmod(0o600)
    temp.replace(path)


class Runtime:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.settings = json.loads(
            (self.root / "migration-settings.private.json").read_text()
        )
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise ValueError("private_owner_directory_required")
        self.local = [
            "limactl",
            "shell",
            self.settings["vm"],
            "--",
            "sudo",
            "/var/lib/rancher/rke2/bin/kubectl",
            "--kubeconfig",
            "/etc/rancher/rke2/rke2.yaml",
            "--request-timeout=20s",
        ]
        self.source = [
            "kubectl",
            "--kubeconfig",
            self.settings["source_kubeconfig"],
            "--request-timeout=20s",
        ]
        self.db = sqlite3.connect(self.root / "migration-effects.sqlite")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS effects (name TEXT PRIMARY KEY, status TEXT, at TEXT)"
        )
        self.db.commit()

    def identity(self, side):
        cmd = self.source if side == "source" else self.local
        observed = command(
            cmd + ["get", "ns", "kube-system", "-o", "jsonpath={.metadata.uid}"]
        )
        if observed != self.settings[side + "_cluster_uid"]:
            raise ValueError("cluster_identity_mismatch")

    def kube(self, side, *args, data=None, binary=False, timeout=120):
        self.identity(side)
        cmd = self.source if side == "source" else self.local
        if data is not None and not isinstance(data, (str, bytes)):
            data = json.dumps(data)
        return command(cmd + list(args), data=data, binary=binary, timeout=timeout)

    def get(self, side, kind, name=None, namespace=None):
        args = ["get", kind]
        if name:
            args.append(name)
        if namespace:
            args += ["-n", namespace]
        return json.loads(self.kube(side, *args, "-o", "json"))

    def effect(self, name, call):
        self.db.execute(
            "INSERT INTO effects VALUES (?, ?, ?)",
            (name, "dispatched", datetime.now(UTC).isoformat()),
        )
        self.db.commit()
        value = call()
        self.db.execute("UPDATE effects SET status='observed' WHERE name=?", (name,))
        self.db.commit()
        return value

    def event(self, stage, detail, **evidence):
        path = self.root / "migration-receipt.private.json"
        doc = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "kind": "local-pc-app-migration.v1",
                "source": "AWS EKS",
                "target": "user-mac-linux-vm",
                "workflow": "explicit_operator",
                "t10_qualified": False,
                "events": [],
            }
        )
        event = {
            "stage": stage,
            "at": datetime.now(UTC).isoformat(),
            "detail": detail,
            "evidence": evidence,
        }
        doc["events"].append(event)
        doc["updated_at"] = event["at"]
        atomic(path, doc)
        print(
            json.dumps({"stage": stage, "detail": detail}, ensure_ascii=False),
            flush=True,
        )

    def create(self, item):
        return json.loads(
            self.kube("target", "create", "-f", "-", "-o", "json", data=item)
        )
