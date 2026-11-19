"""Authenticated, loopback-only personal AWS/VM tunnels; no public ports.

Keeps exact child PIDs and request/log references for explicit cleanup. SSH
host trust is bootstrapped from the verified local Lima VM, then pinned for
this new SSH connection. Does not change global SSH/AWS/kube configuration.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time

from import_target_images import CONFIG, KUBECTL, guest, identity


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--session-manager-plugin", type=Path)
    args = p.parse_args()
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    plugin = args.session_manager_plugin or shutil.which("session-manager-plugin")
    if plugin is None or not Path(plugin).is_file() or not os.access(plugin, os.X_OK):
        raise ValueError("Verified Session Manager plugin required before creating any session")
    plugin = Path(plugin).resolve()
    child_environment = {**os.environ, "PATH": str(plugin.parent) + os.pathsep + os.environ.get("PATH", "")}
    aws = ["aws", "--profile", "default", "--region", "ap-northeast-2"]
    caller = json.loads(subprocess.check_output(aws + ["sts", "get-caller-identity", "--output", "json"]))
    if caller["Account"] != "265233844540":
        raise ValueError("Wrong personal AWS account")
    instance = json.loads(subprocess.check_output(aws + ["ec2", "describe-instances", "--instance-ids",
        "i-04946f0e0c0548c3b", "--output", "json"]))["Reservations"][0]["Instances"][0]
    if {t["Key"]: t["Value"] for t in instance["Tags"]}.get("RunId") != "b0be270e62":
        raise ValueError("Wrong source instance")
    identity()
    ns = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "get", "ns", "sj-b0be270e62", "-o", "json"))
    if ns["metadata"]["uid"] != "565b91c4-4f6e-4d06-94c6-9f8803afa6c9":
        raise ValueError("Destination namespace replaced")
    services = json.loads(guest(KUBECTL, "--kubeconfig", CONFIG, "-n", "sj-b0be270e62", "get", "svc", "-o", "json"))
    ips = {s["metadata"]["name"]: s["spec"]["clusterIP"] for s in services["items"]}
    holds, ports = [], {}
    for kind in ("source_db", "source_app", "target_db", "target_app"):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        ports[kind] = sock.getsockname()[1]
        holds.append(sock)
    config = Path.home() / ".lima/sova-rke2-target/ssh.config"
    match = re.search(r"^  Port ([0-9]+)$", config.read_text(), re.MULTILINE)
    if match is None:
        raise ValueError("Current local Lima SSH endpoint required")
    ssh_port = int(match[1])
    public = guest("cat", "/etc/ssh/ssh_host_ed25519_key.pub").decode().split()
    if len(public) < 2 or public[0] != "ssh-ed25519":
        raise ValueError("Expected VM SSH public key missing")
    known = args.artifacts / "known_hosts"
    known.write_text(f"[127.0.0.1]:{ssh_port} {public[0]} {public[1]}\n")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(public[1])).digest()).decode().rstrip("=")
    for sock in holds:
        sock.close()
    report = {"run_id": "b0be270e62", "ports": ports, "bind": "127.0.0.1",
              "ssh_host_key": fingerprint, "ssh_trust": "verified local Lima VM bootstrap; pinned new connection",
              "ssm_plugin_sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
              "ssm_plugin_version": subprocess.check_output([str(plugin), "--version"], text=True, timeout=5).strip(),
              "children": [], "ready": False, "operational_qualification": False}
    children = []

    def start(name, command):
        with (args.artifacts / (name + ".log")).open("xb") as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True, env=child_environment)
        children.append(child)
        report["children"].append({"name": name, "pid": child.pid})
        (args.artifacts / "connections.json").write_text(json.dumps(report, indent=2) + "\n")

    for kind, remote, port in (("source_db", "10.246.0.10", 5432), ("source_app", "10.246.0.20", 8080)):
        request = {"Target": "i-04946f0e0c0548c3b", "DocumentName": "AWS-StartPortForwardingSessionToRemoteHost",
            "Reason": "Sova personal b0be270e62 " + kind,
            "Parameters": {"host": [remote], "portNumber": [str(port)], "localPortNumber": [str(ports[kind])]}}
        f = args.artifacts / (kind + "-request.json")
        f.write_text(json.dumps(request) + "\n")
        start(kind, aws + ["ssm", "start-session", "--cli-input-json", "file://" + str(f.resolve())])
    start("target_ssh", ["ssh", "-F", str(config), "-o", "StrictHostKeyChecking=yes",
        "-o", "NoHostAuthenticationForLocalhost=no", "-o", "UserKnownHostsFile=" + str(known.resolve()),
        "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "-N",
        "-L", f"127.0.0.1:{ports['target_db']}:{ips['orders-db']}:5432",
        "-L", f"127.0.0.1:{ports['target_app']}:{ips['orders-app']}:8080", "lima-sova-rke2-target"])
    deadline = time.monotonic() + 30
    while True:
        if any(c.poll() is not None for c in children):
            raise RuntimeError("A tunnel stopped; retained PIDs/logs require explicit cleanup")
        opened = []
        for port in ports.values():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    opened.append(port)
            except OSError:
                pass
        if len(opened) == 4:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Tunnel listeners did not become ready; not application/TLS proof")
        time.sleep(0.2)
    report["ready"] = True
    report["listener_only"] = True
    (args.artifacts / "connections.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"listeners": 4, "bind": "127.0.0.1", "tailscale_used": False,
                      "application_tls_qualified": False}))


if __name__ == "__main__":
    main()
