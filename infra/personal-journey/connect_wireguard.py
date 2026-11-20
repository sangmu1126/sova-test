"""Pin the owned VM SSH bridge and verify four end-to-end TLS connections.

This is a controller transport, not migration admission. The source data
connection traverses the already authenticated two-port WireGuard link.
"""

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

import psycopg
from psycopg.conninfo import make_conninfo
import requests
from requests.adapters import HTTPAdapter

from import_target_images import CONFIG, KUBECTL, VM, guest, identity


class TLSName(HTTPAdapter):
    def __init__(self, name):
        self.name = name
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        return super().init_poolmanager(*args, **kwargs, assert_hostname=self.name, server_hostname=self.name)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "wireguard", "artifacts"):
        p.add_argument("--"+name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    inputs = json.loads((a.inputs / "identity.json").read_text())
    if inputs["run_id"] != "b0be270e62" or datetime.now(UTC) >= datetime.fromisoformat(inputs["expires_at"]):
        raise ValueError("Current owned trial required")
    identity()
    applied = json.loads((a.wireguard / "apply-result.json").read_text())
    if applied.get("phase") == "network_created_not_app_migrated":
        if applied.get("applied") is not True or applied.get("passthrough", {}).get("started") is not True:
            raise ValueError("Completed endpoint and proxy receipt required")
    else:
        proxy = json.loads((a.wireguard / "proxy-completion.json").read_text())
        if applied["phase"] != "target_interface_confirmed" or not proxy["result"]["started"]:
            raise ValueError("Expected explicitly completed data connection required")
    state = json.loads(guest("cat", "/run/sova-wireguard-b0be270e62/state.json"))
    live_key = guest("wg", "show", "sova-b0be", "public-key").decode().strip()
    if not state["applied"] or state["public_key"] != applied["target"]["public_key"] or live_key != state["public_key"]:
        raise ValueError("VM WireGuard identity changed")
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    namespace = inputs["namespace"]
    ns = json.loads(guest(KUBECTL,"--kubeconfig",CONFIG,"get","ns",namespace,"-o","json"))
    if ns["metadata"]["uid"] != "565b91c4-4f6e-4d06-94c6-9f8803afa6c9":
        raise ValueError("Wrong namespace")
    svcs = json.loads(guest(KUBECTL,"--kubeconfig",CONFIG,"-n",namespace,"get","svc","-o","json"))
    ips = {s["metadata"]["name"]:s["spec"]["clusterIP"] for s in svcs["items"]}
    config = Path.home()/".lima/sova-rke2-target/ssh.config"
    ssh_port = int(re.search(r"^  Port ([0-9]+)$",config.read_text(),re.MULTILINE)[1])
    key = guest("cat","/etc/ssh/ssh_host_ed25519_key.pub").decode().split()
    fp = "SHA256:"+base64.b64encode(hashlib.sha256(base64.b64decode(key[1])).digest()).decode().rstrip("=")
    if fp != "SHA256:L4P4WpzRQHmIPETcJ9Es0+4z+Ch5IAKGtgoi1hVhU0Q":
        raise ValueError("Pinned VM changed")
    known = a.artifacts/"known_hosts"
    known.write_text(f"[127.0.0.1]:{ssh_port} {key[0]} {key[1]}\n")
    ports = {}
    for name in ("source_db","source_app","target_db","target_app"):
        with socket.socket() as s:
            s.bind(("127.0.0.1",0))
            ports[name]=s.getsockname()[1]
    argv = ["ssh","-F",str(config),"-o","StrictHostKeyChecking=yes","-o","NoHostAuthenticationForLocalhost=no",
        "-o","UserKnownHostsFile="+str(known),"-o","ControlMaster=no","-o","ControlPath=none",
        "-o","ExitOnForwardFailure=yes","-o","ServerAliveInterval=15","-o","ServerAliveCountMax=2","-N"]
    for name, remote, port in (("source_db","10.247.0.1",5432),("source_app","10.247.0.1",8080),
        ("target_db",ips["orders-db"],5432),("target_app",ips["orders-app"],8080)):
        argv += ["-L",f"127.0.0.1:{ports[name]}:{remote}:{port}"]
    argv += ["lima-"+VM]
    with (a.artifacts/"ssh.log").open("xb") as log:
        child = subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    report = {"run_id":inputs["run_id"],"ports":ports,"bind":"127.0.0.1","ssh_host_key":fp,
        "transport":"pinned SSH to owned VM; source over WireGuard","children":[{"name":"wireguard_ssh","pid":child.pid}],
        "ready":False,"operational_qualification":False}
    path = a.artifacts/"connections.json"
    path.write_text(json.dumps(report,indent=2)+"\n")
    try:
        for _ in range(50):
            if child.poll() is not None:
                raise ValueError("SSH stopped; inspect retained log")
            try:
                for port in ports.values():
                    with socket.create_connection(("127.0.0.1",port),timeout=.2):
                        pass
                break
            except OSError:
                time.sleep(.1)
        else:
            raise ValueError("Listeners not ready")
        checks = {}
        for side, expected in (("source","7709720208587968539"),("target","7709723763091251229")):
            item=inputs["environments"][side]
            dsn=make_conninfo(host=item["database_host"],hostaddr="127.0.0.1",port=ports[side+"_db"],dbname=item["database"],
                user="postgres",password=(a.inputs/("client/"+side+"-admin.password")).read_text(),sslmode="verify-full",
                sslrootcert=str(a.inputs/"pki/ca.pem"),connect_timeout=5)
            with psycopg.connect(dsn) as c:
                if str(c.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0]) != expected:
                    raise ValueError("Wrong database")
                checks[side+"_db"]={"verified_tls":True,"counts":list(c.execute("SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)").fetchone())}
            with requests.Session() as s:
                s.trust_env=False
                s.mount("https://",TLSName(item["application_host"]))
                r=s.get(f"https://127.0.0.1:{ports[side+'_app']}/healthz",verify=str(a.inputs/"pki/ca.pem"),timeout=8)
                r.raise_for_status()
                checks[side+"_app"]={"verified_tls":True,"http_status":r.status_code,"body":r.json()}
        denied = json.loads(guest("python3","-c","import json,socket\nr={}\nfor p in (22,6443,5433):\n try:\n  s=socket.create_connection(('10.247.0.1',p),timeout=1);s.close();r[str(p)]=False\n except OSError:r[str(p)]=True\nprint(json.dumps(r))"))
        if not all(denied.values()):
            raise ValueError("Unexpected VPN destination reachable")
        report.update(ready=True,listener_only=False,checks=checks,denied_ports=denied,completed_at=datetime.now(UTC).isoformat())
        path.write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps({"actual_tls_paths":4,"denied_ports":len(denied),"application_route_changed":False}))
    except BaseException:
        child.terminate()
        child.wait(timeout=5)
        raise


if __name__ == "__main__":
    main()
