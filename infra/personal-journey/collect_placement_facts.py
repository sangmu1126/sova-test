"""Read actual storage bindings for this owned trial, never issue residency PASS.

Asset placement is not geographic residence. Database storage, a retained backup
and an authority witness are observed separately. Unenumerated copies, host logs,
retention and AI data handling remain unknown. No registration or worker changes.
"""

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import psycopg
from psycopg.conninfo import make_conninfo


RUN = "b0be270e62"
INSTANCE = "i-04946f0e0c0548c3b"
VOLUME = "vol-0cf101c7982064b83"
VM = "sova-rke2-target"
MACHINE = "a789f623bca040989f282881d9a21654"
CLUSTER = "9de35591-57a3-4759-bd73-6340e2de0b61"
NAMESPACE = "sj-" + RUN
NAMESPACE_UID = "565b91c4-4f6e-4d06-94c6-9f8803afa6c9"
KUBECTL = "/var/lib/rancher/rke2/bin/kubectl"
CONFIG = "/etc/rancher/rke2/rke2.yaml"
MOUNT = "/var/lib/sova-qualification/user-journey-" + RUN + "/mounted"
BACKUP = MOUNT + "/backups/dfb52dbac535/source.dump"
BACKUP_SHA = "9261d2f51f6bcca27f66a0fcbd5d568483e10a8ee72eca8abdca723979e70fd8"
SYSTEMS = {"source": "7709720208587968539", "target": "7709723763091251229"}

SOURCE = r'''
import json,pathlib,subprocess
def call(*args):
 p=subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
 if len(p.stdout)>32768:raise ValueError('Bounded placement projection required')
 return p.stdout
if pathlib.Path('/var/lib/sova-journey/run-id').read_text().strip()!='b0be270e62':raise ValueError('Wrong source')
c=json.loads(call('/usr/local/bin/nerdctl','--namespace','sova-b0be270e62','inspect','orders-db'))[0]
if c['State']['Running'] is not True or (c['Config'].get('Labels') or {}).get('sova.run')!='b0be270e62':raise ValueError('Wrong running database container')
mounts=[x for x in c['Mounts'] if x['Destination']=='/var/lib/postgresql/data']
if len(mounts)!=1 or mounts[0]['Source']!='/var/lib/sova-journey/workload/data':raise ValueError('Unexpected source storage')
found=json.loads(call('findmnt','-J','-T',mounts[0]['Source']))['filesystems'][0]
devices=json.loads(call('lsblk','--json','--output','NAME,TYPE,PKNAME,SERIAL'))['blockdevices']
print(json.dumps({'container_id':c['Id'],'data_mount':{k:mounts[0].get(k) for k in ('Source','Destination','RW','Type')},'filesystem':found,'devices':devices}))
'''

TARGET = r'''
import hashlib,json,pathlib,stat,subprocess
def call(*args):
 p=subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
 if len(p.stdout)>65536:raise ValueError('Bounded target placement projection required')
 return p.stdout
if pathlib.Path('/etc/machine-id').read_text().strip()!='a789f623bca040989f282881d9a21654':raise ValueError('Wrong VM')
k=['/var/lib/rancher/rke2/bin/kubectl','--kubeconfig','/etc/rancher/rke2/rke2.yaml']
def get(kind,name,namespace=None):
 return json.loads(call(*(k+(['-n',namespace] if namespace else [])+['get',kind,name,'-o','json'])))
cluster=get('ns','kube-system')['metadata']['uid'];ns=get('ns','sj-b0be270e62')['metadata']['uid']
if cluster!='9de35591-57a3-4759-bd73-6340e2de0b61' or ns!='565b91c4-4f6e-4d06-94c6-9f8803afa6c9':raise ValueError('Replaced cluster or namespace')
pvc=get('pvc','orders-data','sj-b0be270e62');pv=get('pv',pvc['spec']['volumeName'])
if pv['spec']['claimRef']['uid']!=pvc['metadata']['uid']:raise ValueError('Rebound volume')
pods=json.loads(call(*(k+['-n','sj-b0be270e62','get','pods','-l','sova.run=b0be270e62,sova.role=db','-o','json'])))['items']
if len(pods)!=1:raise ValueError('One actual target database Pod required')
pod=pods[0]
container=next(x for x in pod['spec']['containers'] if x['name']=='postgres')
data=[x for x in container['volumeMounts'] if x['mountPath']=='/var/lib/postgresql/data']
if len(data)!=1 or set(data[0])!={'name','mountPath'}:raise ValueError('Exact actual data mount required')
volumes=[x for x in pod['spec']['volumes'] if x['name']==data[0]['name']]
if len(volumes)!=1 or volumes[0].get('persistentVolumeClaim',{}).get('claimName')!='orders-data':raise ValueError('Pod does not consume the observed PVC')
statuses=[x for x in pod['status']['containerStatuses'] if x['name']=='postgres']
if len(statuses)!=1 or statuses[0]['ready'] is not True:raise ValueError('Observed database is not Ready')
path=(pv['spec'].get('local') or pv['spec'].get('hostPath') or {}).get('path')
mount=pathlib.Path('/var/lib/sova-qualification/user-journey-b0be270e62/mounted')
if path!=str(mount) or (mount/'.sova-encrypted-volume').read_text().strip()!='b0be270e62':raise ValueError('Unexpected retained storage')
found=json.loads(call('findmnt','-J','-T',str(mount)))['filesystems'][0]
if found['target']!=str(mount) or found['source']!='/dev/mapper/sova-b0be270e62':raise ValueError('Encrypted mount unavailable')
files=[]
for category,relative,limit in [('backups','backups/dfb52dbac535/source.dump',1048576),('audit','authority-witness/personal-qualification.log',4194304)]:
 file=mount/relative
 if file.resolve(strict=True)!=file or not stat.S_ISREG(file.lstat().st_mode) or file.stat().st_size>limit:raise ValueError('Unsafe observed file')
 before=file.stat();raw=file.read_bytes();after=file.stat()
 if before.st_dev!=mount.stat().st_dev or (before.st_ino,before.st_dev,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_dev,after.st_size,after.st_mtime_ns):raise ValueError('Observed file changed or is on a nested mount')
 files.append({'category':category,'path':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'device':after.st_dev})
print(json.dumps({'machine_id':'a789f623bca040989f282881d9a21654','cluster_uid':cluster,'namespace_uid':ns,'pod_uid':pod['metadata']['uid'],'runtime_image':statuses[0]['imageID'],'pvc_uid':pvc['metadata']['uid'],'pv_uid':pv['metadata']['uid'],'pv_path':path,'pvc_phase':pvc['status']['phase'],'filesystem':found,'files':files}))
'''


def summarize(source, target, volume, databases):
    """Reject broken identity chains; never infer coverage from a storage label."""
    if (volume.get("VolumeId") != VOLUME or volume.get("Encrypted") is not True
            or len(volume.get("Attachments", [])) != 1
            or volume["Attachments"][0].get("InstanceId") != INSTANCE
            or volume["Attachments"][0].get("State") != "attached"):
        raise ValueError("Exact encrypted source EBS attachment required")
    devices = []

    def visit(items):
        for item in items:
            devices.append(item)
            visit(item.get("children", []))

    visit(source["devices"])
    disks = [d for d in devices if str(d.get("serial", "")).replace("-", "") == VOLUME.replace("-", "")]
    device = source["filesystem"]["source"].removeprefix("/dev/")
    matches = [d for d in devices if d.get("name") == device]
    if (len(disks) != 1 or len(matches) != 1 or
            (matches[0] is not disks[0] and matches[0].get("pkname") != disks[0]["name"])):
        raise ValueError("Source filesystem is not bound to the declared EBS serial")
    if (target.get("machine_id"), target.get("cluster_uid"), target.get("namespace_uid")) != (MACHINE, CLUSTER, NAMESPACE_UID):
        raise ValueError("Wrong physical target")
    if (target.get("pv_path") != MOUNT or target.get("pvc_phase") != "Bound"
            or target["filesystem"].get("target") != MOUNT
            or target["filesystem"].get("source") != "/dev/mapper/sova-" + RUN):
        raise ValueError("Actual PVC and encrypted mount binding required")
    files = target["files"]
    if len(files) != 2 or {f["category"] for f in files} != {"backups", "audit"}:
        raise ValueError("Separate backup and witness observations required")
    backup = next(f for f in files if f["category"] == "backups")
    audit = next(f for f in files if f["category"] == "audit")
    if (backup["path"] != BACKUP or backup["sha256"] != BACKUP_SHA
            or audit["path"] != MOUNT + "/authority-witness/personal-qualification.log"
            or backup["device"] != audit["device"]):
        raise ValueError("Retained category-specific objects changed or relocated")
    for side, system in SYSTEMS.items():
        if databases[side]["system_identifier"] != system or databases[side]["tls"] is not True:
            raise ValueError("Actual TLS database identity required")
        expected_path = "/var/lib/postgresql/data" + ("/pgdata" if side == "target" else "")
        if databases[side]["data_directory"] != expected_path or databases[side]["database"] != side + "_orders":
            raise ValueError("Observed server data directory is not the container mount")
    return {
        "business_data": {"observed": True, "source_ebs": VOLUME, "target_pvc": target["pvc_uid"]},
        "backups": {"observed": True, "archive_sha256": backup["sha256"], "restore_retested": False},
        "audit": {"observed": True, "witness_sha256": audit["sha256"], "all_audit_sinks_observed": False},
        "ai_inputs": {"observed": False, "reason": "model_calls_deferred; no data-handling claim"},
        "all_copies_enumerated": False, "geographic_residency_verified": False,
        "residency_obligation_satisfied": False, "migration_authorized": False,
    }


def main():
    started_code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((a.inputs / "identity.json").read_text())
    links = json.loads(a.connections.read_text())
    if identity["run_id"] != RUN or links["run_id"] != RUN or links["ready"] is not True or datetime.now(UTC) >= datetime.fromisoformat(identity["expires_at"]):
        raise ValueError("Unexpired connected owned trial required")
    cfg = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    if session.client("sts", config=cfg).get_caller_identity()["Account"] != "265233844540":
        raise ValueError("Personal account only")
    ec2 = session.client("ec2", config=cfg)
    instance = ec2.describe_instances(InstanceIds=[INSTANCE])["Reservations"][0]["Instances"][0]
    if instance["State"]["Name"] != "running" or {t["Key"]: t["Value"] for t in instance["Tags"]}.get("RunId") != RUN:
        raise ValueError("Owned source unavailable")
    volume = ec2.describe_volumes(VolumeIds=[VOLUME])["Volumes"][0]
    ssm = session.client("ssm", config=cfg)
    command = "python3 -c \"import base64;exec(base64.b64decode('" + base64.b64encode(SOURCE.encode()).decode() + "'))\""
    cid = ssm.send_command(InstanceIds=[INSTANCE], DocumentName="AWS-RunShellScript", Comment="Read only owned Sova storage identities", Parameters={"commands": [command], "executionTimeout": ["60"]})["Command"]["CommandId"]
    (a.artifacts / "source-command.json").write_text(json.dumps({"command_id": cid, "projection_sha256": hashlib.sha256(SOURCE.encode()).hexdigest()}) + "\n")
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        try:
            result = ssm.get_command_invocation(CommandId=cid, InstanceId=INSTANCE)
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvocationDoesNotExist":
                raise
            time.sleep(0.5)
            continue
        if result["Status"] in {"Success", "Failed", "TimedOut", "Cancelled"}:
            break
        time.sleep(0.5)
    else:
        raise ValueError("Bounded source observation timed out")
    if result["Status"] != "Success":
        (a.artifacts / "source-error.txt").write_text(result.get("StandardErrorContent", ""))
        raise ValueError("Source storage observation failed")
    source = json.loads(result["StandardOutputContent"])
    raw = subprocess.run(["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n", "python3", "-c", TARGET], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90).stdout
    if len(raw) > 65536:
        raise ValueError("Target projection exceeds bound")
    target = json.loads(raw)
    databases = {}
    for side in SYSTEMS:
        env = identity["environments"][side]
        dsn = make_conninfo(host=env["database_host"], hostaddr="127.0.0.1", port=links["ports"][side + "_db"], dbname=env["database"], user="postgres", password=(a.inputs / ("client/" + side + "-admin.password")).read_text(), sslmode="verify-full", sslrootcert=str(a.inputs / "pki/ca.pem"), connect_timeout=5, options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000")
        with psycopg.connect(dsn) as c:
            row = c.execute("SELECT (SELECT system_identifier::text FROM pg_control_system()), (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()), current_setting('data_directory'), current_database()").fetchone()
            databases[side] = dict(zip(("system_identifier", "tls", "data_directory", "database"), row))
    summary = summarize(source, target, volume, databases)
    if datetime.now(UTC) >= datetime.fromisoformat(identity["expires_at"]):
        raise ValueError("Trial expired during observation")
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != started_code:
        raise ValueError("Collector code changed during the observation")
    public_volume = {k: volume[k] for k in ("VolumeId", "Encrypted", "AvailabilityZone")}
    public_volume["attachments"] = [{k: item.get(k) for k in ("InstanceId", "VolumeId", "State", "Device")} for item in volume["Attachments"]]
    report = {"version": 1, "run_id": RUN, "observed_at": datetime.now(UTC).isoformat(), "collector_sha256": started_code, "source": source, "target": target, "source_ebs": public_volume, "databases": databases, "summary": summary, "business_data_or_route_changed": False}
    (a.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
