"""Renew only this personal canary source, preserving its existing PG data.

A new explicit deadline and pinned Secret version are applied. Existing keys,
passwords, scope and image must match. Failure stops the owned instance; never
re-bootstrap storage, replay data mutations or widen network/IAM permissions.
"""

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import time

import boto3
from botocore.config import Config


INSTANCE = "i-04946f0e0c0548c3b"
RUN = "b0be270e62"


def script(version, expires):
    template = r"""set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin
umask 077
state=/var/lib/sova-journey/workload
namespace=sova-b0be270e62
test "$(cat /var/lib/sova-journey/run-id)" = b0be270e62
test "$(aws sts get-caller-identity --region ap-northeast-2 --query Account --output text)" = 265233844540
cat > /etc/systemd/system/sova-expiry.timer <<'SOVA_TIMER'
[Unit]
Description=Explicit renewed personal qualification deadline
[Timer]
OnCalendar=@CALENDAR@ UTC
Persistent=true
Unit=sova-expiry.service
[Install]
WantedBy=timers.target
SOVA_TIMER
systemctl daemon-reload
systemctl restart sova-expiry.timer
systemctl is-active --quiet sova-expiry.timer
nerdctl --namespace "$namespace" inspect orders-db orders-app > "$state/before-renewal-v3.json"
python3 - "$state" <<'VERIFY_IDENTITY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);items=json.loads((root/'before-renewal-v3.json').read_text())
expected={'orders-db':('70:70','sha256:01f80835db480f8403d638051eff0ace599264a908bad70a214b8952173363c5'),
'orders-app':('10001:10001','sha256:f80bc08328b0f962151b949706589bd5db3a12803dfa3096fb700bd3fad5e208')}
if len(items)!=2:raise ValueError('Owned app and DB required')
for item in items:
 name=item['Name'].lstrip('/');user,digest=expected[name]
 if item['Config']['Labels'].get('sova.run')!='b0be270e62' or item['Config']['User']!=user or not item['Config']['Image'].endswith(digest):raise ValueError('Existing workload changed')
VERIFY_IDENTITY
if ! nerdctl --namespace "$namespace" exec orders-db pg_isready -U postgres -d source_orders >/dev/null 2>&1; then
 nerdctl --namespace "$namespace" start orders-db
fi
ready=0
for attempt in {1..30}; do
 if nerdctl --namespace "$namespace" exec orders-db pg_isready -U postgres -d source_orders >/dev/null 2>&1; then ready=1;break;fi
 sleep 1
done
[[ $ready == 1 ]]
identity=$(nerdctl --namespace "$namespace" exec orders-db psql -XAt -U postgres -d source_orders -c "SELECT system_identifier FROM pg_control_system()")
[[ $identity == 7709720208587968539 ]]
rows_before=$(nerdctl --namespace "$namespace" exec orders-db psql -XAt -U postgres -d source_orders -c "SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)")
[[ $rows_before == '1|1|1' ]]
aws secretsmanager get-secret-value --region ap-northeast-2 --secret-id sova/qualification/b0be270e62/source --version-id @VERSION@ --version-stage AWSCURRENT --query SecretString --output text > "$state/credentials-v3.json"
python3 - "$state" <<'RENEW_FILES'
from pathlib import Path
from datetime import datetime,timezone,timedelta
import base64,json,os,sys
root=Path(sys.argv[1]);raw=(root/'credentials-v3.json').read_bytes()
if len(raw)>65536:raise ValueError('Oversized Secret')
j=json.loads(raw)
if set(j)!={'version','run_id','files'} or j['version']!=1 or j['run_id']!='b0be270e62':raise ValueError('Wrong source Secret')
for kind,allowed in {'db':{'ca.pem','server.pem'},'app':{'ca.pem','server.pem','database-ca.pem','config.json'}}.items():
 files=j['files'][kind]
 if set(files)!={p.name for p in (root/kind).iterdir()}:raise ValueError('Mounted inventory changed')
 for name,value in files.items():
  raw=base64.b64decode(value,validate=True);dest=root/kind/name
  if name not in allowed:
   if dest.read_bytes()!=raw:raise ValueError('Key, password or workload input changed')
   continue
  if name=='config.json':
   old,new=json.loads(dest.read_text()),json.loads(raw)
   old.pop('expires_at');until=datetime.fromisoformat(new.pop('expires_at'));now=datetime.now(timezone.utc)
   if old!=new or not now<until<=now+timedelta(hours=8):raise ValueError('Only explicit bounded expiry may change')
  tmp=dest.with_name(dest.name+'.v3-next')
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  uid=70 if kind=='db' else 10001;os.chown(tmp,uid,uid);os.replace(tmp,dest)
RENEW_FILES
nerdctl --namespace "$namespace" exec orders-db psql -XAt -U postgres -d source_orders -c 'SELECT pg_reload_conf()'
nerdctl --namespace "$namespace" restart --time 8 orders-app
python3 - <<'RESTORE_OWNED_BRIDGE'
import base64,hashlib,json,pathlib,subprocess
data=base64.b64decode('@BRIDGE_PROGRAM@',validate=True)
if hashlib.sha256(data).hexdigest()!='@BRIDGE_SHA256@':raise ValueError('Bridge helper changed')
bridge='br-e727195d35bb';chain='SOVA_b0be270e62_H'
addresses=json.loads(subprocess.check_output(['ip','-j','address','show','dev',bridge]))
ipv4=[(a['local'],a['prefixlen']) for a in addresses[0]['addr_info'] if a['family']=='inet']
if not ipv4:
 path=pathlib.Path('/var/lib/sova-journey/source-operator-bridge-@BRIDGE_SHA256@.py')
 if path.exists():
  if path.is_symlink() or path.read_bytes()!=data:raise ValueError('Existing helper differs')
 else:path.write_bytes(data);path.chmod(0o600)
 subprocess.run(['python3',str(path),'--run-id','b0be270e62','--apply'],check=True)
else:
 if ipv4!=[('10.246.0.1',24)]:raise ValueError('Unexpected existing bridge IPv4')
 for args in [
  ['-C','INPUT','-i',bridge,'-s','10.246.0.0/24','-j',chain],
  ['-C','FORWARD','-i',bridge,'!','-o',bridge,'-j','DROP'],
  ['-C',chain,'-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT'],
  ['-C',chain,'-j','DROP']]:subprocess.run(['iptables',*args],check=True)
 print(json.dumps({'existing_operator_bridge_verified':True,'changed':False}))
RESTORE_OWNED_BRIDGE
rows_after=$(nerdctl --namespace "$namespace" exec orders-db psql -XAt -U postgres -d source_orders -c "SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)")
[[ $rows_before == "$rows_after" ]]
printf 'source_data_preserved=%s\n' "$rows_after"
systemctl show sova-expiry.timer -p NextElapseUSecRealtime
"""
    bridge = Path(__file__).with_name("source_operator_bridge.py").read_bytes()
    return (
        template.replace("@VERSION@", version)
        .replace("@CALENDAR@", expires.strftime("%Y-%m-%d %H:%M:%S"))
        .replace("@BRIDGE_PROGRAM@", base64.b64encode(bridge).decode())
        .replace("@BRIDGE_SHA256@", hashlib.sha256(bridge).hexdigest())
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inputs", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((args.inputs / "identity.json").read_text())
    expires = datetime.fromisoformat(identity["expires_at"])
    now = datetime.now(UTC)
    if (
        identity["run_id"] != RUN
        or not now < expires
        or (expires - now).total_seconds() > 28800
    ):
        raise ValueError("Explicit new run-owned window within eight hours required")
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    cfg = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
    if (
        session.client("sts", config=cfg).get_caller_identity()["Account"]
        != "265233844540"
    ):
        raise ValueError("Wrong personal account")
    ec2, ssm = session.client("ec2", config=cfg), session.client("ssm", config=cfg)
    instance = ec2.describe_instances(InstanceIds=[INSTANCE])["Reservations"][0][
        "Instances"
    ][0]
    if {t["Key"]: t["Value"] for t in instance["Tags"]}.get("RunId") != RUN:
        raise ValueError("Source ownership changed")
    secret = session.client("secretsmanager", config=cfg).put_secret_value(
        SecretId="sova/qualification/b0be270e62/source",
        SecretString=(args.inputs / "source-secret.json").read_text(),
    )
    (args.artifacts / "secret-version.json").write_text(
        json.dumps({"version_id": secret["VersionId"], "arn": secret["ARN"]})
    )
    complete = False
    try:
        if instance["State"]["Name"] == "stopped":
            ec2.start_instances(InstanceIds=[INSTANCE])
        elif instance["State"]["Name"] != "running":
            raise ValueError("Source is transitioning; refuse competing renewal")
        deadline = time.monotonic() + 180
        while True:
            rows = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [INSTANCE]}]
            )["InstanceInformationList"]
            if rows and rows[0]["PingStatus"] == "Online":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Owned source did not become SSM Online")
            time.sleep(3)
        command = script(secret["VersionId"], expires)
        (args.artifacts / "renew-source.sh").write_text(command)
        result = ssm.send_command(
            InstanceIds=[INSTANCE],
            DocumentName="AWS-RunShellScript",
            Comment="Explicit data-preserving personal qualification window renewal",
            Parameters={"commands": [command], "executionTimeout": ["120"]},
        )
        cid = result["Command"]["CommandId"]
        (args.artifacts / "command.json").write_text(
            json.dumps({"command_id": cid, "instance": INSTANCE})
        )
        deadline = time.monotonic() + 140
        while True:
            try:
                state = ssm.get_command_invocation(CommandId=cid, InstanceId=INSTANCE)
            except ssm.exceptions.InvocationDoesNotExist:
                state = {"Status": "Pending"}
            if state["Status"] not in ("Pending", "InProgress", "Delayed"):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Bounded renewal command did not settle")
            time.sleep(2)
        (args.artifacts / "invocation.json").write_text(json.dumps(state, default=str))
        if state[
            "Status"
        ] != "Success" or "source_data_preserved=1|1|1" not in state.get(
            "StandardOutputContent", ""
        ):
            raise RuntimeError("Renewal failed; private invocation retained")
        ec2.create_tags(
            Resources=[INSTANCE],
            Tags=[{"Key": "ExpiresAt", "Value": expires.isoformat()}],
        )
        complete = True
        print(
            json.dumps(
                {
                    "instance": INSTANCE,
                    "renewed_until": expires.isoformat(),
                    "data_preserved": True,
                    "role_passwords_or_image_changed": False,
                    "migration_complete": False,
                }
            )
        )
    finally:
        if not complete:
            ec2.stop_instances(InstanceIds=[INSTANCE])
            (args.artifacts / "stopped-after-renewal-failure.json").write_text(
                json.dumps({"instance": INSTANCE, "stop_requested": True})
            )


if __name__ == "__main__":
    main()
