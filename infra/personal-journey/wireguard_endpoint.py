"""Run-owned Linux WireGuard endpoint; standard kernel/tools, no agent keys.

Private keys exist only in root tmpfs and kernel memory. There is no default
route, IP forwarding enablement, arbitrary destination or global firewall flush.
Source traffic terminates at a TCP passthrough on the WG address, not a bridge
route. The target guards return to existing CNI policy for permitted traffic.
"""

import base64
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys

RUN = "b0be270e62"
INTERFACE = "sova-b0be"
ROOT = Path("/run/sova-wireguard-" + RUN)
PREFIX = "SOVA_B0BE_WG_"
UTC = timezone.utc  # The Amazon Linux operator interpreter is Python 3.9.


def call(*args, data=None, check=True):
    if args and args[0] == "iptables":
        args = ("iptables", "--wait", "5", *args[1:])
    return subprocess.run(args, input=data, capture_output=True, check=check, timeout=20)


def public(key):
    value = call("wg", "pubkey", data=key).stdout.decode().strip()
    if len(base64.b64decode(value, validate=True)) != 32:
        raise ValueError("Invalid WireGuard key")
    return value


def identity(side):
    if os.geteuid() != 0 or os.uname().machine not in ("aarch64", "arm64"):
        raise ValueError("Owned ARM64 Linux root endpoint required")
    if side == "source":
        if Path("/var/lib/sova-journey/run-id").read_text().strip() != RUN:
            raise ValueError("Wrong source")
    elif side == "target":
        if (Path("/etc/machine-id").read_text().strip() != "a789f623bca040989f282881d9a21654"
                or Path("/etc/sova-qualification-id").read_text().strip() != "sova-rke2-qualification-v1"):
            raise ValueError("Wrong VM")
    else:
        raise ValueError("Exact side required")


def main():
    os.umask(0o077)
    data = json.load(sys.stdin)
    side, action = data["side"], data["action"]
    identity(side)
    until = datetime.fromisoformat(data["expires_at"])
    if action != "remove" and not datetime.now(UTC) < until <= datetime.now(UTC) + timedelta(hours=8):
        raise ValueError("Existing unexpired bounded trial required")
    key = ROOT / "private.key"
    state = ROOT / "state.json"
    if action == "prepare":
        if call("ip", "link", "show", INTERFACE, check=False).returncode == 0 or ROOT.exists():
            raise ValueError("Existing endpoint requires explicit reconciliation")
        root_mount = json.loads(call("findmnt", "-J", "-T", "/run").stdout)["filesystems"][0]
        if root_mount["fstype"] != "tmpfs":
            raise ValueError("Key staging must be tmpfs")
        call("modprobe", "wireguard")
        routes = json.loads(call("ip", "-j", "route", "show").stdout)
        wanted = ipaddress.ip_network("10.247.0.0/30")
        for route in routes:
            destination = route.get("dst", "default")
            if destination != "default" and wanted.overlaps(ipaddress.ip_network(destination, strict=False)):
                raise ValueError("Dedicated WireGuard subnet overlaps an existing route")
        listeners = call("wg", "show", "all", "listen-port").stdout.decode().splitlines()
        if any(line.split()[-1] == "51820" for line in listeners):
            raise ValueError("WireGuard port already belongs to another endpoint")
        ROOT.mkdir(mode=0o700)
        key.write_bytes(call("wg", "genkey").stdout)
        result = {"run_id": RUN, "side": side, "public_key": public(key.read_bytes()),
                  "expires_at": until.isoformat(), "applied": False, "private_key_persistent": False}
        state.write_text(json.dumps(result)+"\n")
    elif action == "apply":
        result = json.loads(state.read_text())
        if (result["side"] != side or result["applied"] or result["expires_at"] != until.isoformat()
                or result["public_key"] != data["expected_public_key"]
                or public(key.read_bytes()) != result["public_key"]):
            raise ValueError("Endpoint proposal changed")
        peer = data["peer_public_key"]
        if len(base64.b64decode(peer, validate=True)) != 32 or peer == result["public_key"]:
            raise ValueError("Distinct peer key required")
        endpoint = ipaddress.IPv4Address(data["source_public_ip"])
        client = ipaddress.IPv4Address(data["client_public_ip"])
        if not endpoint.is_global or not client.is_global:
            raise ValueError("Current public endpoint/NAT addresses required")
        image = data.get("proxy_image")
        if side == "source" and (not isinstance(image, str)
                or not image.startswith("local.sova.invalid/"+RUN+"/proxy@sha256:")
                or len(image.rsplit(":",1)[1]) != 64
                or any(c not in "0123456789abcdef" for c in image.rsplit(":",1)[1])):
            raise ValueError("Exact run-owned proxy digest required before any change")
        if call("ip", "link", "show", INTERFACE, check=False).returncode == 0:
            raise ValueError("Never overwrite an existing interface")
        chains, hooks = {}, []
        if side == "source":
            chains = {
                "IN": [["-s", "10.247.0.2/32", "-d", "10.247.0.1/32", "-p", "tcp", "-m", "multiport", "--dports", "5432,8080", "-j", "ACCEPT"], ["-j", "DROP"]],
                "OUT": [["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"], ["-j", "DROP"]],
                "UDP": [["-s", str(client)+"/32", "-j", "ACCEPT"], ["-j", "DROP"]],
                "FWD": [["-j", "DROP"]],
            }
            hooks = [("INPUT", ["-i", INTERFACE], "IN"),
                     ("OUTPUT", ["-o", INTERFACE], "OUT"),
                     ("INPUT", ["-p", "udp", "--dport", "51820"], "UDP"),
                     ("FORWARD", ["-i", INTERFACE], "FWD")]
        else:
            chains = {
                "IN": [["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"], ["-j", "DROP"]],
                "OUT": [["-d", "10.247.0.1/32", "-p", "tcp", "-m", "multiport", "--dports", "5432,8080", "-j", "RETURN"], ["-j", "DROP"]],
            }
            hooks = [("INPUT", ["-i", INTERFACE], "IN"),
                     ("FORWARD", ["-i", INTERFACE], "IN"),
                     ("OUTPUT", ["-o", INTERFACE], "OUT"),
                     ("FORWARD", ["-o", INTERFACE], "OUT")]
        for name in chains:
            if call("iptables", "-S", PREFIX+name, check=False).returncode == 0:
                raise ValueError("Existing firewall guard requires reconciliation")
        for name, rules in chains.items():
            call("iptables", "-N", PREFIX+name)
            for rule in rules:
                call("iptables", "-A", PREFIX+name, *rule)
        for table, selector, name in hooks:
            call("iptables", "-I", table, "1", *selector, "-j", PREFIX+name)
        # Configure while DOWN: no interval with an unguarded active interface.
        call("ip", "link", "add", INTERFACE, "type", "wireguard")
        call("ip", "address", "add", "10.247.0.1/30" if side == "source" else "10.247.0.2/30", "dev", INTERFACE)
        args = ["wg", "set", INTERFACE, "private-key", str(key), "listen-port", "51820",
                "peer", peer, "allowed-ips", "10.247.0.2/32" if side == "source" else "10.247.0.1/32"]
        if side == "target":
            args.extend(["endpoint", str(endpoint)+":51820", "persistent-keepalive", "25"])
        call(*args)
        call("ip", "link", "set", "up", "dev", INTERFACE)
        key.unlink()  # Key remains in the kernel, not a persistent file.
        result.update(applied=True, peer_public_key=peer, source_public_ip=str(endpoint),
                      client_cidr=str(client)+"/32", hooks=hooks, chains=list(chains),
                      interface=INTERFACE, forwarding_enabled_by_this_tool=False,
                      default_route_added=False, business_data_changed=False)
        if side == "source":
            result["proxy_image"] = image
        state.write_text(json.dumps(result)+"\n")
    elif action == "observe":
        result = json.loads(state.read_text())
        if not result["applied"] or call("wg", "show", INTERFACE, "public-key").stdout.decode().strip() != result["public_key"]:
            raise ValueError("Live endpoint differs from its owned state")
        result = {**result,
                  "latest_handshakes": call("wg", "show", INTERFACE, "latest-handshakes").stdout.decode().strip(),
                  "transfer": call("wg", "show", INTERFACE, "transfer").stdout.decode().strip(),
                  "key_file_exists": key.exists()}
    elif action == "remove":
        result = json.loads(state.read_text())
        if result.get("run_id") != RUN or result.get("side") != side or not result.get("applied"):
            raise ValueError("Only a completed, owned endpoint may be removed")
        if (call("wg", "show", INTERFACE, "public-key").stdout.decode().strip() != result["public_key"]
                or call("wg", "show", INTERFACE, "peers").stdout.decode().split() != [result["peer_public_key"]]):
            raise ValueError("Refuse removal of a changed interface identity")
        call("ip", "link", "delete", INTERFACE)
        for table, selector, name in result["hooks"]:
            if call("iptables", "-C", table, *selector, "-j", PREFIX+name, check=False).returncode != 0:
                raise ValueError("Owned firewall hook changed; do not flush global state")
            call("iptables", "-D", table, *selector, "-j", PREFIX+name)
        for name in result["chains"]:
            call("iptables", "-F", PREFIX+name)
            call("iptables", "-X", PREFIX+name)
        if key.exists():
            key.unlink()
        if side == "source":
            # No app/DB container is stopped. This new, no-volume connector has
            # a separate namespace and immutable identity.
            base = ["/usr/local/bin/nerdctl", "--namespace", "sova-b0be270e62-vpn"]
            found = call(*base, "inspect", "wireguard-passthrough", check=False)
            if found.returncode == 0:
                container = json.loads(found.stdout)[0]
                labels = container["Config"].get("Labels", {})
                if (labels.get("sova.run") != RUN or labels.get("sova.purpose") != "wireguard-passthrough"
                        or container["Config"]["Image"] != result["proxy_image"]):
                    raise ValueError("Passthrough identity changed; do not stop it")
                if container["State"].get("Running"):
                    call(*base, "stop", "--time", "5", "wireguard-passthrough")
        result.update(applied=False, removed_at=datetime.now(UTC).isoformat())
        state.write_text(json.dumps(result)+"\n")
    else:
        raise ValueError("Only explicit prepare/apply/observe/remove modes supported")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
