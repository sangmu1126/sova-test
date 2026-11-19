"""Actual dedicated SSH witness refusals; no business authority or route change."""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from uuid import uuid4

from import_target_images import VM, guest, identity
from sova.adapters.ssh_authority_witness import SSHAuthorityWitness
from sova.domain.contracts import canonical_json
from sova.service.config import LedgerConfig


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--installation", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    if a.output.exists():
        raise ValueError("Fresh record required")
    record = json.loads((a.installation / "result.json").read_text())
    if datetime.now(UTC) >= datetime.fromisoformat(record["key_expires_at"]):
        raise ValueError("Do not extend an expired key")
    identity()
    socket = Path(record["agent_socket"])
    info = socket.lstat()
    if (socket.is_symlink() or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid() or socket.parent.stat().st_mode & 0o077):
        raise ValueError("Owned private agent socket required")
    os.environ["SSH_AUTH_SOCK"] = str(socket)
    config = a.installation / "ssh-witness.json"
    endpoint = json.loads(config.read_text())["tenants"]["personal-qualification"]
    client = SSHAuthorityWitness(tenant_id="personal-qualification", **endpoint)
    app = "witness-boundary-" + uuid4().hex
    unit = hashlib.sha256(app.encode()).hexdigest()
    document = {"epoch": 1, "test_only": True}
    client.append(app, unit, "probe", document)
    request = {"tenant_id": "personal-qualification", "operation": "latest",
               "application_id": app, "unit_digest": unit}
    raw = canonical_json(request).encode()
    result = {"started_at": datetime.now(UTC).isoformat(), "checks": {}, "passed": False,
              "business_authority_changed": False, "worm_qualified": False}

    def save(name, passed):
        result["checks"][name] = passed
        a.output.write_text(json.dumps(result, indent=2)+"\n")
        if not passed:
            raise RuntimeError("Actual witness boundary failed: " + name)

    command = subprocess.run(client.command + ["printf SOVA_ARBITRARY_COMMAND"],
                             input=raw, capture_output=True, timeout=15)
    reply = json.loads(command.stdout) if not command.returncode else {}
    save("original_shell_command_is_replaced_by_forced_protocol",
         command.returncode == 0 and reply.get("value") == document
         and reply.get("request_digest") == hashlib.sha256(raw).hexdigest())
    forward = subprocess.run(client.command[:-1] + ["-W", "127.0.0.1:6443", client.command[-1]],
                             input=b"", capture_output=True, timeout=10)
    save("stdio_forward_to_kubernetes_refused", forward.returncode != 0
         and b"administratively prohibited" in forward.stderr)
    forbidden = ["/etc/rancher/rke2/rke2.yaml",
                 "/var/lib/sova-qualification/user-journey-b0be270e62/mounted/pgdata"]
    for index, path in enumerate(forbidden):
        probe = subprocess.run(["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n",
                                "runuser", "-u", record["user"], "--", "test", "-r", path],
                               capture_output=True, timeout=15)
        save("kubeconfig_unreadable" if index == 0 else "business_pgdata_unreadable", probe.returncode == 1)
    env = ["env", "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=" + record["code_path"]]
    path = str(Path(record["witness_path"]).with_name("must-not-be-created.log"))
    guard = subprocess.run(["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n",
        "runuser", "-u", record["user"], "--", *env, "python3", "-m", "sova.adapters.ssh_authority_witness",
        "--tenant", "personal-qualification", "--path", path,
        "--mount-root", "/var/lib/sova-qualification/user-journey-b0be270e62/mounted",
        "--mapper", "/dev/mapper/wrong-registered-volume"], input=raw, capture_output=True, timeout=15)
    save("wrong_encrypted_mapper_refused_before_file_access", guard.returncode == 1
         and b"witness_request_refused" in guard.stderr
         and guest("test", "!", "-e", path) == b"")
    native = LedgerConfig("unused", "registry-attestor-1", "handoff-verifier-1", "issuer",
                          "ssh-file:" + str(config), {}).witness_for()("personal-qualification")
    save("native_ledger_backend_reads_same_actual_record", native.latest(app, unit) == document)
    result.update(passed=True, completed_at=datetime.now(UTC).isoformat(),
                  implementation_digest=record["implementation_digest"],
                  probe_application_id=app, probe_unit_digest=unit,
                  driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    a.output.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps({"passed": True, "checks": result["checks"], "worm_qualified": False}))


if __name__ == "__main__":
    main()
