"""Prepare/apply a narrowly scoped personal WG + HAProxy data connection.

Preparation installs signed distribution tools and stages endpoint keys in
tmpfs; no SG/interface/firewall/route change. Apply requires the exact reviewed
proposal and unchanged endpoint code/NAT/source identity. No app DB or app
Deployment is changed. Existing SSM remains the management channel.
"""

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from import_target_images import VM, guest, identity
from prepare_canary import artifact


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("prepare", "review", "apply", "observe"))
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--proxy-image", type=Path)
    p.add_argument("--proxy-scan", type=Path)
    p.add_argument("--resume", action="store_true", help="Recheck a staged key; never regenerate it")
    a = p.parse_args()
    os.umask(0o077)
    identity()
    inputs = json.loads((a.inputs / "identity.json").read_text())
    until = datetime.fromisoformat(inputs["expires_at"])
    if inputs["run_id"] != "b0be270e62" or datetime.now(UTC) >= until:
        raise ValueError("Unexpired dedicated personal trial required")
    cfg = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1, "mode": "standard"})
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    caller = session.client("sts", config=cfg).get_caller_identity()
    if caller["Account"] != "265233844540":
        raise ValueError("Never use another AWS account")
    ec2, ssm, s3 = (session.client(n, config=cfg) for n in ("ec2", "ssm", "s3"))
    instance = ec2.describe_instances(InstanceIds=["i-04946f0e0c0548c3b"])["Reservations"][0]["Instances"][0]
    if ({x["Key"]: x["Value"] for x in instance["Tags"]}.get("RunId") != inputs["run_id"]
            or instance["State"]["Name"] != "running" or len(instance["SecurityGroups"]) != 1):
        raise ValueError("Wrong or changed dedicated source")
    group_id = instance["SecurityGroups"][0]["GroupId"]
    group = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    attached = ec2.describe_network_interfaces(Filters=[{"Name":"group-id","Values":[group_id]}])["NetworkInterfaces"]
    if ({t["Key"]:t["Value"] for t in group.get("Tags", [])}.get("RunId") != inputs["run_id"]
            or any(n.get("Attachment",{}).get("InstanceId") != instance["InstanceId"] for n in attached)
            or not attached):
        raise ValueError("The security group is not exclusive to this owned source")
    source_ip = str(ipaddress.IPv4Address(instance["PublicIpAddress"]))
    peer_ip = guest("python3", "-c", "import urllib.request;print(urllib.request.urlopen('https://checkip.amazonaws.com',timeout=8).read(64).decode().strip())").decode().strip()
    if not ipaddress.IPv4Address(peer_ip).is_global:
        raise ValueError("Current NAT IPv4 required")
    endpoint = Path(__file__).with_name("wireguard_endpoint.py").read_text()
    endpoint_hash = hashlib.sha256(endpoint.encode()).hexdigest()

    def source(stage, code, payload, budget=60):
        program = "import base64,io,json,sys;sys.stdin=io.StringIO(base64.b64decode('" + base64.b64encode(json.dumps(payload).encode()).decode() + "').decode());exec(base64.b64decode('" + base64.b64encode(code.encode()).decode() + "'))"
        command = ssm.send_command(InstanceIds=[instance["InstanceId"]], DocumentName="AWS-RunShellScript",
            Comment="Owned Sova WG " + stage,
            Parameters={"commands": ["python3 -c \"" + program + "\""], "executionTimeout": [str(budget)]})["Command"]["CommandId"]
        (a.artifacts / (stage+"-"+command+".json")).write_text(json.dumps({"id": command, "program_sha256": hashlib.sha256(code.encode()).hexdigest()})+"\n")
        deadline = time.monotonic() + budget + 20
        while time.monotonic() < deadline:
            try:
                value = ssm.get_command_invocation(CommandId=command, InstanceId=instance["InstanceId"])
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "InvocationDoesNotExist":
                    raise
                time.sleep(0.5)
                continue
            if value["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Source command unresolved; inspect before any repeat")
        if value["Status"] != "Success":
            (a.artifacts / (stage+"-failure.txt")).write_text(value.get("StandardErrorContent", "")[-4000:])
            raise RuntimeError("Source stage failed; bounded private diagnostic kept: " + stage)
        return json.loads(value["StandardOutputContent"])

    def target(code, payload, budget=60):
        response = subprocess.run(["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n", "python3", "-c", code],
            input=json.dumps(payload).encode(), capture_output=True, timeout=budget)
        if response.returncode:
            (a.artifacts / "target-failure.txt").write_bytes(response.stderr[-4000:])
            raise RuntimeError("Target stage failed; bounded private diagnostic kept")
        return json.loads(response.stdout)

    base = {"expires_at": until.isoformat()}
    if a.mode == "prepare":
        a.artifacts.mkdir(mode=0o700, exist_ok=a.resume)
        if not a.proxy_image or not a.proxy_scan:
            raise ValueError("Exact inspected proxy artifact and fresh scan required")
        proxy = artifact(a.proxy_image)
        scan = json.loads(a.proxy_scan.read_text())
        if any(row.get("Vulnerabilities") for row in scan.get("Results", [])):
            raise ValueError("Never stage a proxy with unresolved findings")
        # This helper is not an artifact upload endpoint: both paths belong to
        # this trusted operator qualification, and the archive is content-bound.
        bucket = "sova-journey-b0be270e62-265233844540"
        tags = {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}
        if tags.get("RunId") != inputs["run_id"]:
            raise ValueError("Wrong artifact bucket")
        key = "source/images/" + proxy["archive_sha256"] + "/canary.oci.tar"
        try:
            old = s3.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchKey"):
                raise
            old = None
        if old is None:
            s3.upload_file(str(a.proxy_image / "canary.oci.tar"), bucket, key,
                           ExtraArgs={"Metadata": {"sha256": proxy["archive_sha256"]}})
        elif old["Metadata"].get("sha256") != proxy["archive_sha256"]:
            raise ValueError("Existing content-addressed object metadata differs")
        source_install = """import json,pathlib,subprocess,sys
c=json.load(sys.stdin)
if pathlib.Path('/var/lib/sova-journey/run-id').read_text().strip()!='b0be270e62':raise ValueError('Wrong source')
subprocess.run(['dnf','-y','--setopt=install_weak_deps=False','install','wireguard-tools-1.0.20210914-4.amzn2023.aarch64'],stdout=subprocess.DEVNULL,check=True,timeout=120)
sys.stdin=__import__('io').StringIO(json.dumps(c))
""" + endpoint
        prepared = a.artifacts / "source-prepared.json"
        if a.resume and prepared.exists():
            source_state = json.loads(prepared.read_text())
            inspect = """import json,pathlib,subprocess
r=pathlib.Path('/run/sova-wireguard-b0be270e62');s=json.loads((r/'state.json').read_text())
if s['applied'] or subprocess.check_output(['wg','pubkey'],input=(r/'private.key').read_bytes()).decode().strip()!=s['public_key']:raise ValueError('Staged key or phase changed')
print(json.dumps(s))
"""
            if source("source-resume-readback", inspect, {}) != source_state:
                raise ValueError("Staged source identity differs; do not recreate it")
        else:
            source_state = source("source-prepare", source_install, {**base, "side": "source", "action": "prepare"}, 150)
            with prepared.open("x") as f:
                f.write(json.dumps(source_state, indent=2)+"\n")
        target_install = """import json,pathlib,subprocess,sys
c=json.load(sys.stdin)
if pathlib.Path('/etc/machine-id').read_text().strip()!='a789f623bca040989f282881d9a21654':raise ValueError('Wrong VM')
subprocess.run(['env','DEBIAN_FRONTEND=noninteractive','apt-get','-o','DPkg::Lock::Timeout=60','install','--assume-yes','--no-install-recommends','wireguard-tools=1.0.20210914-1ubuntu4'],stdout=subprocess.DEVNULL,check=True,timeout=120)
sys.stdin=__import__('io').StringIO(json.dumps(c))
""" + endpoint
        target_state = target(target_install, {**base, "side": "target", "action": "prepare"}, 150)
        plan = {"run_id": inputs["run_id"], "source_instance": instance["InstanceId"],
                "source_ip": source_ip, "client_ip": peer_ip, "source_group_id": group_id,
                "expires_at": until.isoformat(), "endpoint_code_sha256": endpoint_hash,
                "source": source_state, "target": target_state, "proxy": proxy,
                "artifact_bucket": bucket, "artifact_key": key, "applied": False,
                "public_ingress": {"protocol": "udp", "port": 51820, "cidr": peer_ip+"/32"},
                "vpn_tcp_destinations": ["10.247.0.1:5432", "10.247.0.1:8080"],
                "public_tcp_added": False, "ip_forwarding_enabled": False,
                "existing_app_or_db_changed": False}
        (a.artifacts / "plan.json").write_text(json.dumps(plan, indent=2)+"\n")
        print(json.dumps({"prepared": True, "network_applied": False, "new_public_udp_peer_prefix": 32,
                          "public_tcp_added": False, "private_keys_persistent": False}))
        return
    plan_path = a.artifacts / ("plan.json" if a.mode == "review" else "reviewed-plan.json")
    plan = json.loads(plan_path.read_text())
    if (plan["run_id"] != inputs["run_id"] or plan["source_ip"] != source_ip or plan["client_ip"] != peer_ip
            or plan["source_group_id"] != group_id
            or plan["expires_at"] != until.isoformat()):
        raise ValueError("Reviewed proposal/source/NAT/code changed")
    if a.mode == "review":
        if (a.artifacts / "reviewed-plan.json").exists() or group["IpPermissions"]:
            raise ValueError("Fresh review and unchanged empty ingress required")
        inspect = """import json,pathlib,subprocess
r=pathlib.Path('/run/sova-wireguard-b0be270e62');s=json.loads((r/'state.json').read_text())
if s['applied']:raise ValueError('Already applied')
if subprocess.check_output(['wg','pubkey'],input=(r/'private.key').read_bytes()).decode().strip()!=s['public_key']:raise ValueError('Staged key changed')
print(json.dumps(s))
"""
        live_source, live_target = source("source-review", inspect, {}), target(inspect, {})
        if live_source != plan["source"] or live_target != plan["target"]:
            raise ValueError("Prepared endpoint identity changed")
        reviewed = dict(plan, prepared_endpoint_code_sha256=plan["endpoint_code_sha256"],
                        endpoint_code_sha256=endpoint_hash, reviewed_at=datetime.now(UTC).isoformat(),
                        review_scope="Autonomous personal infrastructure maintenance authorized by user; not product approval")
        with (a.artifacts / "reviewed-plan.json").open("x") as f:
            f.write(json.dumps(reviewed, indent=2)+"\n")
        print(json.dumps({"reviewed":True,"network_applied":False,"changed_prepared_code":endpoint_hash!=plan["endpoint_code_sha256"]}))
        return
    if plan["endpoint_code_sha256"] != endpoint_hash:
        raise ValueError("Endpoint changed after review")
    if a.mode == "observe":
        observed = {side: (source("source-observe", endpoint, {**base,"side":side,"action":"observe"})
                          if side == "source" else target(endpoint, {**base,"side":side,"action":"observe"}))
                    for side in ("source", "target")}
        (a.artifacts / "observation.json").write_text(json.dumps(observed, indent=2)+"\n")
        print(json.dumps({"observed_endpoints": len(observed)}))
        return
    if (a.artifacts / "apply-result.json").exists() or group["IpPermissions"]:
        raise ValueError("Existing ingress/apply requires readback, never replay an uncertain change")
    permission = {"IpProtocol":"udp","FromPort":51820,"ToPort":51820,
                  "IpRanges":[{"CidrIp":peer_ip+"/32","Description":"Sova b0be270e62 authenticated WireGuard peer only"}]}
    before = {"phase":"before_ingress","at":datetime.now(UTC).isoformat(),"applied":False,
              "application_data_or_route_changed":False,"public_tcp_added":False}
    receipt = a.artifacts / "apply-result.json"
    receipt.write_text(json.dumps(before,indent=2)+"\n")
    change = ec2.authorize_security_group_ingress(GroupId=group_id,IpPermissions=[permission],
        TagSpecifications=[{"ResourceType":"security-group-rule","Tags":[
            {"Key":"RunId","Value":inputs["run_id"]},{"Key":"Purpose","Value":"wireguard-personal-data-plane"}]}])
    before.update(phase="ingress_confirmed",ingress_rule_ids=[x["SecurityGroupRuleId"]for x in change["SecurityGroupRules"]])
    receipt.write_text(json.dumps(before,indent=2)+"\n")
    proxy_ref = "local.sova.invalid/b0be270e62/proxy@"+plan["proxy"]["manifest_digest"]
    for side in ("source","target"):
        payload={**base,"side":side,"action":"apply","expected_public_key":plan[side]["public_key"],
                 "peer_public_key":plan["target" if side=="source" else "source"]["public_key"],
                 "source_public_ip":source_ip,"client_public_ip":peer_ip,"proxy_image":proxy_ref}
        live = source("source-apply",endpoint,payload) if side=="source" else target(endpoint,payload)
        before[side]=live
        before["phase"]=side+"_interface_confirmed"
        receipt.write_text(json.dumps(before,indent=2)+"\n")
    # TCP passthrough: TLS stays end-to-end at the app/PG server. No forwarding
    # exception or plaintext DB listener is added to the public interface.
    proxy_code = """import hashlib,json,pathlib,subprocess,sys
c=json.load(sys.stdin)
if pathlib.Path('/var/lib/sova-journey/run-id').read_text().strip()!='b0be270e62':raise ValueError('Wrong source')
root=pathlib.Path('/var/lib/sova-journey/wireguard-passthrough');root.mkdir(mode=0o700,exist_ok=False)
archive=root/'proxy.oci.tar'
subprocess.run(['aws','s3','cp','s3://'+c['bucket']+'/'+c['key'],str(archive),'--region','ap-northeast-2','--only-show-errors'],check=True)
digest=hashlib.sha256()
with archive.open('rb')as f:
 while True:
  block=f.read(1024*1024)
  if not block:break
  digest.update(block)
if digest.hexdigest()!=c['sha256']:raise ValueError('Image archive changed')
ns='sova-b0be270e62-vpn';base=['/usr/local/bin/nerdctl','--namespace',ns]
subprocess.run(['/usr/local/bin/ctr','--namespace',ns,'images','import','--base-name','local.sova.invalid/b0be270e62/proxy','--digests',str(archive)],check=True,stdout=subprocess.DEVNULL)
cfg=root/'haproxy.cfg';cfg.write_text('global\\n  maxconn 128\\n  nbthread 1\\ndefaults\\n  mode tcp\\n  timeout connect 3s\\n  timeout client 5m\\n  timeout server 5m\\nfrontend database\\n  bind 10.247.0.1:5432\\n  default_backend database\\nbackend database\\n  server db 10.246.0.10:5432\\nfrontend application\\n  bind 10.247.0.1:8080\\n  default_backend application\\nbackend application\\n  server app 10.246.0.20:8080\\n');cfg.chmod(0o444)
mount='type=bind,src='+str(cfg)+',dst=/usr/local/etc/haproxy/haproxy.cfg,ro'
subprocess.run(base+['run','--detach','--name','wireguard-passthrough','--label','sova.run=b0be270e62','--label','sova.purpose=wireguard-passthrough',
 '--network','host','--read-only','--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges',
 '--memory','64m','--pids-limit','64','--cpus','0.25','--mount',mount,c['image'],'haproxy','-W','-db','-f','/usr/local/etc/haproxy/haproxy.cfg'],check=True,stdout=subprocess.DEVNULL)
print(json.dumps({'started':True,'image':c['image'],'public_tcp_listener':False,'tls_terminated':False,'data_ports':[5432,8080]}))
"""
    before["passthrough"]=source("source-passthrough",proxy_code,{"bucket":plan["artifact_bucket"],"key":plan["artifact_key"],
        "sha256":plan["proxy"]["archive_sha256"],"image":proxy_ref},120)
    before.update(phase="network_created_not_app_migrated",applied=True,completed_at=datetime.now(UTC).isoformat())
    receipt.write_text(json.dumps(before,indent=2)+"\n")
    print(json.dumps({"network_created":True,"application_route_changed":False,"next":"actual TLS, denials, expiry and reconnect qualification"}))


if __name__ == "__main__":
    main()
