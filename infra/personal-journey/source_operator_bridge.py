"""Host-only access to this run's internal bridge, keeping container egress shut.

No public listener, default container route, SG ingress or global firewall
policy change. New host-directed container requests and forwarded egress are
dropped; replies to host-originated SSM connections remain possible.
"""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess


def call(*args, check=True):
    return subprocess.run(args, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if (os.geteuid() != 0 or not re.fullmatch("[a-z0-9]{8,12}", args.run_id)
            or Path("/var/lib/sova-journey/run-id").read_text().strip() != args.run_id):
        raise ValueError("Wrong run-owned source")
    ns = "sova-" + args.run_id
    network = json.loads(call("/usr/local/bin/nerdctl", "--namespace", ns, "network", "inspect", ns).stdout)[0]
    if (network["Name"] != ns or network["Id"] != "e727195d35bb4abced2505b309d2a1fd9e938f3fc02e80e40f151b68d08ef31c"
            or network["IPAM"]["Config"] != [{"Subnet": "10.246.0.0/24", "Gateway": "10.246.0.1"}]
            or {c["Name"] for c in network["Containers"].values()} != {"orders-app", "orders-db"}):
        raise ValueError("Wrong dedicated source bridge")
    bridge = "br-" + network["Id"][:12]
    address = json.loads(call("ip", "-j", "address", "show", "dev", bridge).stdout)[0]
    if any(a["family"] == "inet" for a in address["addr_info"]):
        raise ValueError("Bridge already has IPv4; inspect earlier changes instead of replay")
    chain = "SOVA_" + args.run_id + "_H"
    if call("iptables", "-S", chain, check=False).returncode == 0:
        raise ValueError("Earlier firewall chain exists; reconcile instead of overwriting")
    result = {"run_id": args.run_id, "bridge": bridge, "host_address": "10.246.0.1/24",
        "public_listener_added": False, "container_default_route_added": False,
        "new_container_to_host_requests": "DROP", "forwarded_container_egress": "DROP", "applied": False}
    if args.apply:
        os.umask(0o077)
        root = Path("/var/lib/sova-journey")
        (root / "firewall-before-operator-bridge.txt").write_bytes(call("iptables-save").stdout)
        call("iptables", "-N", chain)
        call("iptables", "-A", chain, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
        call("iptables", "-A", chain, "-j", "DROP")
        call("iptables", "-I", "INPUT", "1", "-i", bridge, "-s", "10.246.0.0/24", "-j", chain)
        call("iptables", "-I", "FORWARD", "1", "-i", bridge, "!", "-o", bridge, "-j", "DROP")
        call("ip", "address", "add", "10.246.0.1/24", "dev", bridge)
        with socket.create_connection(("10.246.0.10", 5432), timeout=3):
            result["host_to_db_tcp"] = True
        with socket.create_connection(("10.246.0.20", 8080), timeout=3):
            result["host_to_app_tcp"] = True
        result["applied"] = True
        (root / "operator-bridge.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
