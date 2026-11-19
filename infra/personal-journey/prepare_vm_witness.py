"""Prepare/test the existing file witness on the owned encrypted VM.

Creates one non-sudo forced-command account. Its key lives in the designated
Mac Keychain item and an expiring private ssh-agent; only the public identity
file is written locally. Does not move or erase an existing witness history.
"""

import argparse
import base64
from datetime import UTC, datetime
import hashlib
from importlib import import_module
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from import_target_images import VM, guest, identity
from sova.adapters.ssh_authority_witness import SSHAuthorityWitness
from sova.domain.contracts import ContractError
from sova.service.directional_catalog import PACKAGE, source_identity


INSTALL = r'''
import base64,hashlib,io,json,os,pathlib,pwd,subprocess,sys,tarfile
c=json.load(sys.stdin);os.umask(0o077)
if pathlib.Path('/etc/machine-id').read_text().strip()!='a789f623bca040989f282881d9a21654':
 raise ValueError('Wrong VM')
mount=pathlib.Path('/var/lib/sova-qualification/user-journey-b0be270e62/mounted')
p=subprocess.run(['findmnt','-J','-T',str(mount)],capture_output=True,check=True)
f=json.loads(p.stdout)['filesystems'][0]
if f['target']!=str(mount) or f['source']!='/dev/mapper/sova-b0be270e62':raise ValueError('LUKS mount missing')
if (mount/'.sova-encrypted-volume').read_text().strip()!='b0be270e62':raise ValueError('Wrong run')
root=pathlib.Path('/var/lib/sova-qualification/user-journey-b0be270e62')
if root.is_symlink() or root.stat().st_uid!=0 or root.stat().st_mode&0o077 not in (0,0o011):
 raise ValueError('Unexpected run-directory ownership or permissions')
# Traversal only; no directory listing or writes. Existing secret/data files
# keep their restrictive modes and witness storage is dedicated-user 0700.
root.chmod(0o711)
code=root/('witness-code-'+c['identity']['digest'])
if code.exists():raise ValueError('Fresh immutable code destination required')
code.mkdir(mode=0o755)
raw=base64.b64decode(c['archive'],validate=True)
if hashlib.sha256(raw).hexdigest()!=c['archive_sha256']:raise ValueError('Source archive mismatch')
with tarfile.open(fileobj=io.BytesIO(raw))as t:
 members=t.getmembers()
 if len(members)!=len(c['identity']['files']):raise ValueError('Unexpected source entries')
 for item in members:
  if not item.isfile() or item.name not in c['identity']['files']:raise ValueError('Only measured regular sources allowed')
  content=t.extractfile(item).read()
  if hashlib.sha256(content).hexdigest()!=c['identity']['files'][item.name]:raise ValueError('Source bytes differ')
  p=code/item.name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content);p.chmod(0o444)
for p in sorted(code.rglob('*'),reverse=True):
 if p.is_dir():p.chmod(0o555)
code.chmod(0o555)
user='sova-witness-b0be'
try:pwd.getpwnam(user);raise ValueError('Dedicated user already exists; do not overwrite an active endpoint')
except KeyError:pass
home=root/'witness-home';home.mkdir(mode=0o755);(home/'.ssh').mkdir(mode=0o755)
subprocess.run(['useradd','--system','--user-group','--home-dir',str(home),'--shell','/bin/sh',user],check=True,capture_output=True)
account=pwd.getpwnam(user)
storage=mount/'authority-witness';storage.mkdir(mode=0o700);os.chown(storage,account.pw_uid,account.pw_gid)
path=storage/'personal-qualification.log'
command='env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH='+str(code)+' /usr/bin/python3 -m sova.adapters.ssh_authority_witness --tenant personal-qualification --path '+str(path)+' --mount-root '+str(mount)+' --mapper /dev/mapper/sova-b0be270e62'
authorized=home/'.ssh/authorized_keys'
authorized.write_text('restrict,command="'+command+'" '+c['public_key']+'\n');authorized.chmod(0o600)
print(json.dumps({'user':user,'uid':account.pw_uid,'gid':account.pw_gid,'code_path':str(code),
 'implementation_digest':c['identity']['digest'],'witness_path':str(path),'encrypted_mapper':f['source'],
 'forced_command':True,'key_restrict':True,'sudo_granted':False,'worm_qualified':False}))
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    inputs = json.loads((a.inputs / "identity.json").read_text())
    end = datetime.fromisoformat(inputs["expires_at"])
    if inputs["run_id"] != "b0be270e62" or not 60 < (end - datetime.now(UTC)).total_seconds() <= 28800:
        raise ValueError("Unexpired owned trial required")
    identity()
    sys.path.insert(0, "/Users/Sooming_/dev/softbank_2026/personal-development/.local-deps/personal-keychain")
    from keyring.backends.macOS import Keyring

    ring = Keyring()
    service, account = "sova.personal.witness.b0be270e62", "transport-v1"
    private = ring.get_password(service, account)
    if private is None:
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                    serialization.NoEncryption()).decode()
        ring.set_password(service, account, private)
        if ring.get_password(service, account) != private:
            raise ValueError("Keychain write readback failed")
    key = serialization.load_ssh_private_key(private.encode(), None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Expected dedicated Ed25519 key")
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    public_file = a.artifacts / "identity.pub"
    public_file.write_text(public + "\n")
    public_file.chmod(0o400)
    agent_socket = a.artifacts / "agent.sock"
    if len(str(agent_socket)) > 100:
        # macOS AF_UNIX paths are short. A private run-specific /tmp directory
        # contains only a socket, never private key bytes.
        import tempfile

        socket_dir = Path(tempfile.mkdtemp(prefix="sova-witness-", dir="/private/tmp"))
        agent_socket = socket_dir / "agent.sock"
    with (a.artifacts / "agent.log").open("xb") as log:
        agent = subprocess.Popen(["/usr/bin/ssh-agent", "-D", "-a", str(agent_socket)],
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    ready = False
    try:
        for _ in range(50):
            if agent.poll() is not None:
                raise RuntimeError("Dedicated agent stopped")
            if agent_socket.is_socket():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Dedicated agent did not open")
        env = dict(os.environ, SSH_AUTH_SOCK=str(agent_socket))
        subprocess.run(["/usr/bin/ssh-add", "-t", str(int((end-datetime.now(UTC)).total_seconds())), "-"],
                       input=private.encode(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       check=True, timeout=10)
        measured = source_identity()
        roots = {"sova": PACKAGE, "sova_security": Path(import_module("sova_security").__file__).resolve().parent}
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name, expected in measured["files"].items():
                prefix, path = name.split("/", 1)
                raw = (roots[prefix] / path).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("Code changed during snapshot")
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(raw), 0o444
                tar.addfile(info, io.BytesIO(raw))
        payload = {"identity": measured, "public_key": public,
                   "archive": base64.b64encode(archive.getvalue()).decode(),
                   "archive_sha256": hashlib.sha256(archive.getvalue()).hexdigest()}
        remote = subprocess.run(["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n", "python3", "-c", INSTALL],
                                input=json.dumps(payload).encode(), capture_output=True, timeout=120)
        if remote.returncode:
            (a.artifacts / "install-failure.txt").write_bytes(remote.stderr[-3000:])
            raise RuntimeError("Witness install failed; private diagnostic retained")
        installed = json.loads(remote.stdout)
        config = (Path.home() / ".lima/sova-rke2-target/ssh.config").read_text()
        port = int(re.search(r"^  Port ([0-9]+)$", config, re.MULTILINE)[1])
        host_key = guest("cat", "/etc/ssh/ssh_host_ed25519_key.pub").decode().split()
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(host_key[1])).digest()).decode().rstrip("=")
        if fingerprint != "SHA256:L4P4WpzRQHmIPETcJ9Es0+4z+Ch5IAKGtgoi1hVhU0Q":
            raise ValueError("Existing pinned VM host changed")
        known = a.artifacts / "known_hosts"
        known.write_text(f"[127.0.0.1]:{port} {host_key[0]} {host_key[1]}\n")
        endpoint = dict(host="127.0.0.1", port=port, user=installed["user"],
                        key_file=str(public_file), known_hosts=str(known))
        os.environ["SSH_AUTH_SOCK"] = str(agent_socket)
        witness = SSHAuthorityWitness(tenant_id=inputs["tenant_id"], **endpoint)
        unit = hashlib.sha256(b"dedicated-personal-witness-probe-b0be270e62").hexdigest()
        app = "witness-probe-b0be270e62"
        if witness.latest(app, unit) is not None:
            raise ValueError("Probe unexpectedly has history")
        witness.append(app, unit, "probe", {"epoch": 1, "test_only": True})
        fresh = SSHAuthorityWitness(tenant_id=inputs["tenant_id"], **endpoint)
        if fresh.latest(app, unit) != {"epoch": 1, "test_only": True}:
            raise ValueError("Fresh SSH readback disagreed")
        try:
            SSHAuthorityWitness(tenant_id="other", **endpoint).latest(app, unit)
        except ContractError:
            denied = True
        else:
            raise ValueError("Other tenant reached witness")
        result = dict(installed, checks={"actual_append_and_fresh_readback": True,
                                        "cross_tenant_refused": denied},
                      host_key_fingerprint=fingerprint, agent_pid=agent.pid,
                      agent_socket=str(agent_socket), key_expires_at=end.isoformat(),
                      private_key_reference="macos-keychain:"+service+"/"+account,
                      private_key_written_to_file=False, application_authority_changed=False,
                      completed_at=datetime.now(UTC).isoformat())
        (a.artifacts / "ssh-witness.json").write_text(json.dumps({"tenants": {inputs["tenant_id"]: endpoint}}, indent=2)+"\n")
        (a.artifacts / "result.json").write_text(json.dumps(result, indent=2)+"\n")
        ready = True
        print(json.dumps({"checks": result["checks"], "private_key_file": False, "worm_qualified": False}))
    finally:
        if not ready:
            agent.terminate()
            agent.wait(timeout=5)


if __name__ == "__main__":
    main()
