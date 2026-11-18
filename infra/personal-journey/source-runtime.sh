#!/bin/bash
# Run only through authenticated SSM on a new, empty, run-owned source.
# No credential values, application deployment or existing service changes.
set -euo pipefail
umask 077
run_id=${1:?explicit run ID required}
[[ $run_id =~ ^[a-z0-9]{8,12}$ ]]
[[ $(id -u) == 0 && $(uname -m) == aarch64 ]]
[[ $(cat /var/lib/sova-journey/run-id) == "$run_id" ]]
systemctl is-active --quiet sova-expiry.timer
account=$(aws sts get-caller-identity --region ap-northeast-2 --query Account --output text)
[[ $account == 265233844540 ]]
if command -v containerd >/dev/null || command -v nerdctl >/dev/null; then
  echo 'Refusing to overwrite an existing runtime' >&2
  exit 1
fi

digest=2deffe14625f93866f90a8f265d0f59d6fa476c7dc54b98adb327b1b4613efd4
archive=nerdctl-full-2.4.1-linux-arm64.tar.gz
directory=/var/lib/sova-journey/runtime
install -d -m 0700 "$directory"
aws s3 cp "s3://sova-journey-${run_id}-${account}/source/runtime/${digest}/${archive}" \
  "$directory/$archive" --region ap-northeast-2 --only-show-errors
echo "$digest  $directory/$archive" | sha256sum --check --status

# Extract only regular runtime files from the authenticated vendor archive.
# The remote source does not need BuildKit, a build daemon or a Docker socket.
python3 - "$directory/$archive" "$directory/unpacked" <<'PY'
from pathlib import Path
import sys, tarfile
root = Path(sys.argv[2])
names = {
    'bin/containerd', 'bin/containerd-shim-runc-v2', 'bin/ctr', 'bin/runc', 'bin/nerdctl',
    'lib/systemd/system/containerd.service',
    *('libexec/cni/' + n for n in (
        'bridge', 'loopback', 'host-local', 'portmap', 'firewall', 'tuning')),
}
with tarfile.open(sys.argv[1]) as archive:
    listed = [m.name for m in archive.getmembers()]
    if len(listed) != len(set(listed)):
        raise ValueError('Duplicate runtime archive members')
    for name in names:
        member = archive.getmember(name)
        if not member.isfile() or not 0 < member.size <= 128 * 1024 * 1024:
            raise ValueError('Bounded regular runtime member required')
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open('xb') as destination:
            destination.write(archive.extractfile(member).read())
        path.chmod(0o700)
PY

# Vendor-signed OS packages, no new repositories or signature bypass.
dnf -y install iptables iproute
install -d -m 0755 /usr/local/bin /usr/local/libexec/cni
for binary in containerd containerd-shim-runc-v2 ctr runc nerdctl; do
  install -m 0755 "$directory/unpacked/bin/$binary" "/usr/local/bin/$binary"
done
for plugin in bridge loopback host-local portmap firewall tuning; do
  install -m 0755 "$directory/unpacked/libexec/cni/$plugin" "/usr/local/libexec/cni/$plugin"
done
install -m 0644 "$directory/unpacked/lib/systemd/system/containerd.service" \
  /etc/systemd/system/containerd.service
systemctl daemon-reload
systemctl enable --now containerd
systemctl is-active --quiet containerd
systemctl is-active --quiet sova-expiry.timer
/usr/local/bin/containerd --version
/usr/local/bin/nerdctl --version
/usr/local/bin/runc --version
echo 'Runtime installed; no application or database deployed'
