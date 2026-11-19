"""Diagnose the owned VM API via a pinned SSH bridge and direct Lima port.

No public listener, server configuration, workload or policy changes. The
existing route-reader SA receives one ten-minute token. Repeated actual
Service/EndpointSlice reads qualify only this transport, never migration.
"""

import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time

from sova.adapters.kubernetes.handoff_controls import KubernetesHandoffControls
from sova.domain.contracts import ContractError
from sova.domain.discovery import KubernetesReadRegistration


PROGRAM = """import base64,json,pathlib,subprocess
assert pathlib.Path('/etc/machine-id').read_text().strip()=='a789f623bca040989f282881d9a21654'
assert pathlib.Path('/etc/sova-qualification-id').read_text().strip()=='sova-rke2-qualification-v1'
k=['/var/lib/rancher/rke2/bin/kubectl','--kubeconfig','/etc/rancher/rke2/rke2.yaml','--request-timeout=12s']
def call(args):return subprocess.check_output(k+args,stderr=subprocess.PIPE,timeout=20)
cluster=json.loads(call(['get','ns','kube-system','-o','json']))['metadata']['uid'];assert cluster=='9de35591-57a3-4759-bd73-6340e2de0b61'
ns=json.loads(call(['get','ns','sj-b0be270e62','-o','json']));assert ns['metadata']['uid']=='565b91c4-4f6e-4d06-94c6-9f8803afa6c9'
token=call(['-n','sj-b0be270e62','create','token','orders-route-worker','--duration=10m']).decode().strip()
print(json.dumps({'cluster_uid':cluster,'namespace_uid':ns['metadata']['uid'],'token':token,'ca':base64.b64encode(pathlib.Path('/var/lib/rancher/rke2/server/tls/server-ca.crt').read_bytes()).decode()}))
"""


def main():
    import base64

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--known-hosts", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    inputs = json.loads((a.inputs / "identity.json").read_text())
    end = datetime.fromisoformat(inputs["expires_at"])
    if inputs["run_id"] != "b0be270e62" or datetime.now(UTC) >= end:
        raise ValueError("Unexpired owned personal run required")
    a.output.mkdir(mode=0o700, exist_ok=False)
    # Reuse the already pinned known_hosts from actual WireGuard connection acceptance.
    ssh = [
        "ssh",
        "-F",
        str(Path.home() / ".lima/sova-rke2-target/ssh.config"),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "NoHostAuthenticationForLocalhost=no",
        "-o",
        "UserKnownHostsFile=" + str(a.known_hosts),
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
    ]
    result = subprocess.run(
        [
            *ssh,
            "lima-sova-rke2-target",
            shlex.join(["sudo", "-n", "python3", "-c", PROGRAM]),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    identity = json.loads(result.stdout)
    ca = base64.b64decode(identity.pop("ca"), validate=True)
    (a.output / "ca.pem").write_bytes(ca)
    (a.output / "route.token").write_text(identity.pop("token"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with (a.output / "ssh.log").open("xb") as log:
        child = subprocess.Popen(
            [
                *ssh,
                "-N",
                "-L",
                f"127.0.0.1:{port}:127.0.0.1:6443",
                "lima-sova-rke2-target",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    report = {
        "run_id": inputs["run_id"],
        **identity,
        "port": port,
        "endpoint": f"https://127.0.0.1:{port}",
        "bind": "127.0.0.1",
        "transport": "pinned VM SSH; verified API TLS",
        "children": [{"name": "kubernetes_api_ssh", "pid": child.pid}],
        "ready": False,
        "migration_complete": False,
        "checks": {},
    }
    (a.output / "connection.json").write_text(json.dumps(report, indent=2) + "\n")
    try:
        for _ in range(40):
            if child.poll() is not None:
                raise ValueError("Pinned SSH bridge stopped")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        now = datetime.now(UTC)
        reg = KubernetesReadRegistration(
            tenant_id=inputs["tenant_id"],
            application_id=inputs["application_id"],
            environment_id="local-vm-b0be270e62",
            location="registered-personal-vm",
            endpoint=report["endpoint"],
            approved_addresses=("127.0.0.1",),
            ca_digest=hashlib.sha256(ca).hexdigest(),
            namespace=inputs["namespace"],
            namespace_uid=identity["namespace_uid"],
            revision=1,
            registered_at=now,
            valid_until=min(end, now + timedelta(minutes=9)),
        )
        token = (a.output / "route.token").read_text()
        for name, registration in (
            ("ssh", reg),
            ("lima", replace(reg, endpoint="https://127.0.0.1:19753")),
        ):
            samples = []
            controls = KubernetesHandoffControls(
                registration, ca_pem=ca, token_provider=lambda: token
            )
            for _ in range(5):
                started = time.monotonic()
                try:
                    service = controls.service("orders-entry")
                    if (
                        service["metadata"]["uid"]
                        != "c5565701-1035-4140-aaf1-e6442a89b2d9"
                    ):
                        raise ValueError("Entry service identity changed")
                    controls.endpoints("orders-entry")
                    samples.append(
                        {
                            "passed": True,
                            "duration_ms": round(
                                (time.monotonic() - started) * 1000, 3
                            ),
                        }
                    )
                except ContractError:
                    samples.append(
                        {
                            "passed": False,
                            "reason": "actual_api_read_failed",
                            "duration_ms": round(
                                (time.monotonic() - started) * 1000, 3
                            ),
                        }
                    )
            report["checks"][name] = samples
        report["valid_until"] = reg.valid_until.isoformat()
        report["ready"] = all(s["passed"] for s in report["checks"]["ssh"])
    finally:
        report["completed_at"] = datetime.now(UTC).isoformat()
        (a.output / "connection.json").write_text(json.dumps(report, indent=2) + "\n")
        if not report["ready"]:
            child.terminate()
            child.wait(timeout=8)
    print(
        json.dumps(
            {
                "ssh_ready": report["ready"],
                "samples": {
                    k: sum(x["passed"] for x in v) for k, v in report["checks"].items()
                },
                "migration_complete": False,
            }
        )
    )
    if not report["ready"]:
        raise ValueError(
            "Actual API transport did not qualify; never relax the request budget"
        )


if __name__ == "__main__":
    main()
