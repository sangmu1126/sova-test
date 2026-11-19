"""Create one NEW encrypted canary volume on the verified personal Linux VM.

Reads exactly 64 random key bytes from stdin. No key file is written on the
VM. Never formats an existing path, arbitrary block device or existing mapper.
This is run-scoped LUKS, not enterprise KMS/escrow/HA qualification.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


CLUSTER_UID = "9de35591-57a3-4759-bd73-6340e2de0b61"
MACHINE_ID = "a789f623bca040989f282881d9a21654"
KUBECTL = "/var/lib/rancher/rke2/bin/kubectl"


def run(*args, data=None, check=True):
    return subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=check, timeout=120, env={**os.environ, "LC_ALL": "C"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if (os.geteuid() != 0 or platform.machine() != "aarch64"
            or not re.fullmatch(r"[a-z0-9]{8,12}", args.run_id)
            or Path("/etc/machine-id").read_text().strip() != MACHINE_ID
            or Path("/etc/sova-qualification-id").read_text().strip() != "sova-rke2-qualification-v1"):
        raise ValueError("Wrong personal qualification VM")
    uid = run(KUBECTL, "--kubeconfig", "/etc/rancher/rke2/rke2.yaml", "get", "ns", "kube-system",
              "-o", "jsonpath={.metadata.uid}").stdout.decode()
    if uid != CLUSTER_UID:
        raise ValueError("Wrong destination cluster")
    key = sys.stdin.buffer.read(65)
    if len(key) != 64:
        raise ValueError("Exactly 64 random key bytes required on stdin")
    root = Path("/var/lib/sova-qualification/user-journey-" + args.run_id)
    root.mkdir(mode=0o700, exist_ok=False)
    image, mount = root / "data.luks", root / "mounted"
    mapper_name = "sova-" + args.run_id
    mapper = Path("/dev/mapper") / mapper_name
    if mapper.exists():
        raise ValueError("Never replace an existing device mapping")
    fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.truncate(1024 * 1024 * 1024)
        file.flush()
        os.fsync(file.fileno())
    run("cryptsetup", "luksFormat", "--type", "luks2", "--cipher", "aes-xts-plain64",
        "--key-size", "512", "--pbkdf", "argon2id", "--pbkdf-memory", "262144",
        "--iter-time", "1000", "--batch-mode", "--key-file", "-", str(image), data=key)
    run("cryptsetup", "open", "--type", "luks", "--key-file", "-", str(image), mapper_name, data=key)
    status = run("cryptsetup", "status", mapper_name).stdout.decode()
    device = next((line.split(":", 1)[1].strip() for line in status.splitlines()
                   if line.strip().startswith("device:")), None)
    loops = json.loads(run("losetup", "--list", "--json", "--output", "NAME,BACK-FILE").stdout)
    owns = any(loop.get("name") == device and Path(loop.get("back-file", "")).resolve() == image
               for loop in loops.get("loopdevices", []))
    if not owns:
        raise ValueError("Refusing mkfs: mapping is not the new run-owned backing file")
    # Destructive only to the just-created, identity-checked encrypted file.
    run("mkfs.ext4", "-q", "-F", "-L", mapper_name, str(mapper))
    mount.mkdir(mode=0o000)
    run("mount", "-o", "nodev,nosuid,noexec", str(mapper), str(mount))
    mount.chmod(0o755)
    (mount / ".sova-encrypted-volume").write_text(args.run_id + "\n")
    (mount / ".sova-encrypted-volume").chmod(0o444)
    data = mount / "pgdata"
    data.mkdir(mode=0o700)
    os.chown(data, 70, 70)
    failed_key = run("cryptsetup", "open", "--test-passphrase", "--key-file", "-", str(image),
                     data=bytes(64), check=False)
    if failed_key.returncode == 0:
        raise ValueError("Unexpected wrong-key acceptance")
    metadata = json.loads(run("cryptsetup", "luksDump", "--dump-json-metadata", str(image)).stdout)
    segment, slot = metadata["segments"]["0"], metadata["keyslots"]["0"]
    result = {"run_id": args.run_id, "cluster_uid": uid, "machine_id": MACHINE_ID,
        "backing_file": str(image), "mount": str(mount), "mapper": mapper_name,
        "luks_uuid": run("cryptsetup", "luksUUID", str(image)).stdout.decode().strip(),
        "type": "LUKS2", "cipher": segment["encryption"], "key_bits": slot["key_size"] * 8,
        "pbkdf": slot["kdf"]["type"], "wrong_key_rejected": True,
        "key_written_on_vm": False, "new_backing_file_checked_before_mkfs": owns,
        "volume_reclaim": "Retain; explicit unmount/lock after stopping owned workloads",
        "operational_qualification": False}
    (root / "volume.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
