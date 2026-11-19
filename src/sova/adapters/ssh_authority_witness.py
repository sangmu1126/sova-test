"""A fixed, forced-command SSH transport for the existing file witness.

The server owns its tenant and path; callers cannot choose either. SSH must use
a dedicated key restricted to this command with forwarding/PTY disabled and a
pinned known_hosts file. No retry after a lost append response: the witness may
already have advanced, requiring reconciliation. This preserves file-witness
limitations: it is not WORM storage or independent failure-domain protection.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.domain.contracts import ContractError, canonical_json, digest, identifier
from sova.domain.json_documents import json_document

LIMIT = 65536


def _exchange(command, raw, timeout):
    """Bound the bytes consumed as well as the deadline; never collect logs."""
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as child:
        try:
            deadline, offset, result = time.monotonic() + timeout, 0, bytearray()
            with selectors.DefaultSelector() as events:
                for stream in (child.stdin, child.stdout):
                    os.set_blocking(stream.fileno(), False)
                events.register(child.stdin, selectors.EVENT_WRITE)
                events.register(child.stdout, selectors.EVENT_READ)
                while events.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for key, _ in events.select(min(left, 0.2)):
                        if key.fileobj is child.stdin:
                            offset += os.write(child.stdin.fileno(), raw[offset:offset + 4096])
                            if offset == len(raw):
                                events.unregister(child.stdin)
                                child.stdin.close()
                        else:
                            block = os.read(child.stdout.fileno(), min(4096, LIMIT + 1 - len(result)))
                            if not block:
                                events.unregister(child.stdout)
                            else:
                                result.extend(block)
                                if len(result) > LIMIT:
                                    raise ContractError("Witness response exceeded bound")
            code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
            return code, bytes(result)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)


def handle(document, witness):
    if type(document) is not dict or set(document) not in (
        {"tenant_id", "operation", "application_id", "unit_digest"},
        {"tenant_id", "operation", "application_id", "unit_digest", "action", "record"},
    ):
        raise ContractError("Exact witness request fields required")
    app = identifier(document["application_id"])
    unit = digest(document["unit_digest"])
    if document["tenant_id"] != witness.tenant_id:
        raise ContractError("Witness belongs to another tenant")
    if document["operation"] == "latest" and len(document) == 4:
        value = witness.latest(app, unit)
    elif document["operation"] == "append" and len(document) == 6:
        witness.append(app, unit, document["action"], document["record"])
        value = None
    else:
        raise ContractError("Only fixed latest and append operations are available")
    return {"request_digest": hashlib.sha256(canonical_json(document, limit=LIMIT).encode()).hexdigest(),
            "value": value}


class SSHAuthorityWitness:
    def __init__(self, *, host, port, user, key_file, known_hosts, tenant_id,
                 ssh="/usr/bin/ssh", timeout=15):
        # Host/user are deployment configuration, not request values. Restrict
        # them anyway so OpenSSH option parsing and hostname expansion cannot
        # turn a configured endpoint into another command or proxy.
        import re

        if (type(host) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host)
                or type(user) is not str or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user)
                or type(port) is not int or not 1 <= port <= 65535
                or type(timeout) is not int or not 1 <= timeout <= 30):
            raise ContractError("Fixed bounded SSH endpoint required")
        for name in (key_file, known_hosts):
            p = Path(name)
            if not p.is_absolute() or p.is_symlink() or not p.is_file():
                raise ContractError("Regular absolute SSH credential/trust files required")
        if Path(key_file).stat().st_mode & 0o077:
            raise ContractError("Witness private key permissions must exclude group/other")
        self.tenant_id = identifier(tenant_id)
        self.timeout = timeout
        self.command = [ssh, "-F", "/dev/null", "-T", "-p", str(port),
                        "-i", str(key_file), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                        "-o", "StrictHostKeyChecking=yes", "-o", "NoHostAuthenticationForLocalhost=no",
                        "-o", "UserKnownHostsFile=" + str(known_hosts), "-o", "GlobalKnownHostsFile=/dev/null",
                        "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
                        "-o", "ConnectionAttempts=1", "-o", "ConnectTimeout=5", user + "@" + host]

    def _call(self, request):
        raw = canonical_json(request, limit=LIMIT)
        try:
            code, output = _exchange(self.command, raw.encode(), self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise ContractError("Witness transport unavailable; no automatic retry") from None
        if code or len(output) > LIMIT:
            raise ContractError("Witness transport refused or response exceeded bound")
        try:
            response = json_document(output.decode(), LIMIT)
        except (UnicodeError, ValueError):
            raise ContractError("Invalid witness response") from None
        expected = hashlib.sha256(raw.encode()).hexdigest()
        if (type(response) is not dict or set(response) != {"request_digest", "value"}
                or response["request_digest"] != expected
                or (response["value"] is not None and type(response["value"]) is not dict)):
            raise ContractError("Witness response is not bound to this request")
        return response["value"]

    def append(self, application_id, unit_digest, action, document):
        result = self._call({"tenant_id": self.tenant_id, "operation": "append", "application_id": application_id,
                             "unit_digest": unit_digest, "action": action, "record": document})
        if result is not None:
            raise ContractError("Unexpected witness append response")

    def latest(self, application_id, unit_digest):
        return self._call({"tenant_id": self.tenant_id, "operation": "latest", "application_id": application_id,
                           "unit_digest": unit_digest})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tenant", required=True)
    p.add_argument("--path", type=Path, required=True)
    p.add_argument("--mount-root", type=Path)
    p.add_argument("--mapper")
    a = p.parse_args()
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT or a.path.is_symlink() or not a.path.parent.is_dir():
            raise ContractError("Invalid witness input or fixed storage path")
        if (a.mount_root is None) != (a.mapper is None):
            raise ContractError("Mount root and mapper must be bound together")
        if a.mount_root is not None:
            if (not a.mount_root.is_absolute() or a.mount_root.resolve() != a.mount_root
                    or not a.path.resolve().is_relative_to(a.mount_root)):
                raise ContractError("Witness must stay within its registered mount")
            mount = subprocess.run(["findmnt", "-J", "-T", str(a.path.parent)],
                                   capture_output=True, check=True, timeout=3)
            filesystems = json_document(mount.stdout.decode(), 16384)["filesystems"]
            if (len(filesystems) != 1 or filesystems[0]["target"] != str(a.mount_root)
                    or filesystems[0]["source"] != a.mapper):
                raise ContractError("Registered encrypted mount is unavailable")
        document = json_document(raw.decode(), LIMIT)
        result = handle(document, FileAuthorityWitness(a.path, tenant_id=a.tenant))
        print(json.dumps(result, separators=(",", ":")))
    except (ContractError, ValueError, OSError, KeyError, subprocess.SubprocessError):
        print("witness_request_refused", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
