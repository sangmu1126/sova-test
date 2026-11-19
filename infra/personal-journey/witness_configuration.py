"""Re-use only the measured, current, private personal VM witness endpoint."""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat
import subprocess

from sova.domain.contracts import ContractError
from sova.service.config import LedgerConfig


def witness_environment(directory, inputs):
    directory = Path(directory).resolve(strict=True)
    result = json.loads((directory / "result.json").read_text())
    expires = datetime.fromisoformat(result["key_expires_at"])
    if (not datetime.now(UTC) < expires <= datetime.fromisoformat(inputs["expires_at"])
            or result["user"] != "sova-witness-b0be"
            or result["host_key_fingerprint"] != "SHA256:L4P4WpzRQHmIPETcJ9Es0+4z+Ch5IAKGtgoi1hVhU0Q"
            or not all(result["checks"].values())):
        raise ContractError("Current measured VM witness required")
    socket = Path(result["agent_socket"])
    info = socket.lstat()
    if (socket.is_symlink() or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or socket.parent.stat().st_mode & 0o077):
        raise ContractError("Owned private expiring witness agent required")
    config = directory / "ssh-witness.json"
    document = json.loads(config.read_text())
    if set(document.get("tenants", {})) != {inputs["tenant_id"]}:
        raise ContractError("Witness tenant scope mismatch")
    endpoint = document["tenants"][inputs["tenant_id"]]
    if (endpoint["host"] != "127.0.0.1" or endpoint["user"] != result["user"]
            or Path(endpoint["key_file"]).resolve() != directory / "identity.pub"
            or Path(endpoint["known_hosts"]).resolve() != directory / "known_hosts"):
        raise ContractError("Witness endpoint differs from its measured installation")
    env = {"SSH_AUTH_SOCK": str(socket), "SOVA_WITNESS": "ssh-file:" + str(config)}
    keys = subprocess.run(["/usr/bin/ssh-add", "-L"], env={**os.environ, **env},
                          capture_output=True, check=True, timeout=5).stdout.decode().splitlines()
    expected = (directory / "identity.pub").read_text().split()[:2]
    if len(keys) != 1 or keys[0].split()[:2] != expected:
        raise ContractError("Agent does not hold exactly the registered witness identity")
    LedgerConfig("unused", "registry-attestor-1", "handoff-verifier-1", "issuer",
                 env["SOVA_WITNESS"], {}).witness_for()(inputs["tenant_id"])
    return env
