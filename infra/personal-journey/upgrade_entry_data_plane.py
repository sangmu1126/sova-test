"""Move only the owned entry connector from SSM to verified WireGuard.

Imports the inspected proxy digest, creates a new immutable source config,
and CAS-updates the two entry Deployments. The application Service selector,
application Deployments, credentials and database contents are not changed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from import_target_images import CONFIG, CTR, KUBECTL, SOCKET, VM, guest, identity
from prepare_canary import artifact
from renew_entry import get, patch
from renew_target import mutate


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("proxy-image", "connections", "artifacts"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--resume",action="store_true",help="Read back the exact interrupted connector update; never restart or overwrite it")
    a=p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700,exist_ok=a.resume)
    links=json.loads(a.connections.read_text())
    if not links.get("ready") or links.get("transport") != "pinned SSH to owned VM; source over WireGuard":
        raise ValueError("Actual verified WireGuard TLS paths required")
    identity()
    entry=get("service","orders-entry")
    selector={"sova.run":"b0be270e62","sova.role":"edge","sova.side":"source"}
    if entry["metadata"]["uid"]!="c5565701-1035-4140-aaf1-e6442a89b2d9" or entry["spec"]["selector"]!=selector:
        raise ValueError("Migration started or entry identity changed")
    image=artifact(a.proxy_image)
    if image["manifest_digest"]!="sha256:36cbd2f5e9db6d6a6a012baabf58dcf77a2aba30d31792e140951e693674b30e":
        raise ValueError("Only the freshly inspected fixed proxy is accepted")
    remote="/tmp/sova-b0be270e62-vpn-proxy.oci.tar"
    subprocess.run(["limactl","copy",str(a.proxy_image/"canary.oci.tar"),VM+":"+remote],capture_output=True,check=True,timeout=90)
    if guest("sha256sum",remote).decode().split()[0]!=image["archive_sha256"]:
        raise ValueError("Transferred image differs")
    guest(CTR,"--address",SOCKET,"-n","k8s.io","images","import","--base-name","local.sova.invalid/b0be270e62/proxy","--digests",remote)
    for key in ("manifest_digest","config_digest"):
        blob=guest(CTR,"--address",SOCKET,"-n","k8s.io","content","get",image[key])
        if "sha256:"+hashlib.sha256(blob).hexdigest()!=image[key]:
            raise ValueError("Runtime content differs")
    config=get("configmap","orders-entry-source-v3")
    old=config["data"]["haproxy.cfg"]
    if old.count("192.168.5.2:50741")!=1:
        raise ValueError("Expected SSM upstream changed")
    updated=old.replace("192.168.5.2:50741","10.247.0.1:8080")
    name="orders-entry-source-wg-"+hashlib.sha256(updated.encode()).hexdigest()[:12]
    new={"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":name,"namespace":"sj-b0be270e62",
        "labels":{"sova.run":"b0be270e62","sova.scope":"personal-qualification"}},"immutable":True,"data":{"haproxy.cfg":updated}}
    receipt=a.artifacts/"result.json"
    if a.resume:
        existing=get("configmap",name)
        if existing.get("immutable") is not True or existing["data"]!=new["data"] or existing["metadata"]["labels"].get("sova.run")!="b0be270e62":
            raise ValueError("Existing immutable connector configuration differs")
        report=json.loads(receipt.read_text())
        if report["config"]!=name or report["image"]!=image:
            raise ValueError("Interrupted update belongs to a different candidate")
    else:
        mutate(new,"create","--dry-run=server","-f","-","-o","name")
        mutate(new,"create","-f","-","-o","name")
        report={"config":name,"image":image,"deployment_patches":[],"service_selector_changed":False,"business_data_changed":False}
    receipt.write_text(json.dumps(report,indent=2)+"\n")
    policy=get("networkpolicy","entry-registered-upstreams")
    old_rule=policy["spec"]["egress"][0]
    wanted_rule={"to":[{"ipBlock":{"cidr":"10.247.0.1/32"}}],"ports":[{"protocol":"TCP","port":8080}]}
    if old_rule==wanted_rule and a.resume:
        pass
    elif old_rule!={"to":[{"ipBlock":{"cidr":"192.168.5.2/32"}}],"ports":[{"protocol":"TCP","port":50741}]}:
        raise ValueError("Expected old entry egress changed")
    else:
        patch("networkpolicy","entry-registered-upstreams",policy,[{"op":"test","path":"/spec/egress/0","value":old_rule},
            {"op":"replace","path":"/spec/egress/0","value":wanted_rule}])
    for side in ("source","target"):
        target="orders-entry-"+side
        deployment=get("deployment",target)
        spec=deployment["spec"]["template"]["spec"]
        if deployment["metadata"]["labels"].get("sova.run")!="b0be270e62" or len(spec["containers"])!=1:
            raise ValueError("Wrong entry deployment")
        old_image=spec["containers"][0]["image"]
        wanted_image="local.sova.invalid/b0be270e62/proxy@"+image["manifest_digest"]
        if a.resume and old_image==wanted_image:
            if side=="source" and not any(v.get("configMap",{}).get("name")==name for v in spec["volumes"]):
                raise ValueError("Image changed without the expected source config")
            guest(KUBECTL,"--kubeconfig",CONFIG,"-n","sj-b0be270e62","rollout","status","deployment/"+target,"--timeout=50s")
            if target not in report["deployment_patches"]:
                report["deployment_patches"].append(target)
            receipt.write_text(json.dumps(report,indent=2)+"\n")
            continue
        if not old_image.endswith("@sha256:c3c44f88235cdcc7c5e39a1097811fa156626f0adfb99680d1f3a43011af2503"):
            raise ValueError("Expected previous proxy image changed")
        changes=[{"op":"test","path":"/spec/template/spec/containers/0/image","value":old_image},
            {"op":"replace","path":"/spec/template/spec/containers/0/image","value":wanted_image}]
        if side=="source":
            i=next(i for i,v in enumerate(spec["volumes"]) if v["name"]=="config")
            field=f"/spec/template/spec/volumes/{i}/configMap/name"
            if spec["volumes"][i]["configMap"]["name"]!="orders-entry-source-v3":
                raise ValueError("Source config reference changed")
            changes += [{"op":"test","path":field,"value":"orders-entry-source-v3"},{"op":"replace","path":field,"value":name}]
        patch("deployment",target,deployment,changes)
        guest(KUBECTL,"--kubeconfig",CONFIG,"-n","sj-b0be270e62","rollout","status","deployment/"+target,"--timeout=50s")
        report["deployment_patches"].append(target)
        receipt.write_text(json.dumps(report,indent=2)+"\n")
    after=get("service","orders-entry")
    if after["metadata"]["uid"]!=entry["metadata"]["uid"] or after["metadata"]["resourceVersion"]!=entry["metadata"]["resourceVersion"] or after["spec"]["selector"]!=selector:
        raise ValueError("Service changed during connector update")
    print(json.dumps({"connectors_updated":2,"entry_service_unchanged":True,"application_migrated":False}))


if __name__=="__main__":
    main()
