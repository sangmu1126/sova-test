"""Read actual personal canary dependencies; no completeness/signature inferred.

Reads only named run-owned infrastructure, projected container metadata and
SQL catalog/privilege facts. Never outputs password hashes, app credentials,
Secret values, SQL query text or whole container environments. A later verifier
must decide the declared coverage; this collector never sets complete=True.
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
from botocore.exceptions import ClientError
import psycopg
from psycopg.conninfo import make_conninfo

from import_target_images import CONFIG, KUBECTL, guest, identity as verify_vm

SOURCE_PROJECTION = r'''
import hashlib,json,pathlib,subprocess
if pathlib.Path('/var/lib/sova-journey/run-id').read_text().strip()!='b0be270e62':
 raise ValueError('Wrong dedicated source')
base=['/usr/local/bin/nerdctl','--namespace','sova-b0be270e62']
def call(args):return subprocess.check_output(args,stderr=subprocess.PIPE)
found=json.loads(call(base+['inspect','orders-app','orders-db']))
containers=[]
for c in found:
 config=c.get('Config') or {};state=c.get('State') or {};host=c.get('HostConfig') or {}
 containers.append({'id':c.get('Id'),'name':c.get('Name'),'runtime_image':c.get('Image'),
  'registered_image':config.get('Image'),'run_id':(config.get('Labels') or {}).get('sova.run'),
  'user':config.get('User'),'running':state.get('Running'),'readonly_rootfs':host.get('ReadonlyRootfs'),
  'restart_policy':host.get('RestartPolicy')})
program="""import json,hashlib,pathlib
from sova.qualification.order_runtime import load_configuration
c=load_configuration()
files=['qualification/order_app.py','qualification/order_runtime.py','qualification/order_schema.py','qualification/requests.py','service/http_boundary.py']
print(json.dumps({'tenant':c.tenant,'application':c.application,'run':c.run,'side':c.side,'role':c.role,
'database':c.database,'image_digest':c.image_digest,'expires_at':c.expires_at.isoformat(),
'source_hashes':{f:hashlib.sha256((pathlib.Path('/app/sova')/f).read_bytes()).hexdigest() for f in files}}))"""
app=json.loads(call(base+['exec','orders-app','python','-c',program]))
network=json.loads(call(base+['network','inspect','sova-b0be270e62']))[0]
print(json.dumps({'containers':containers,'application_projection':app,
 'network':{'id':network.get('Id'),'name':network.get('Name'),'internal':network.get('Internal'),
 'ipam':network.get('IPAM'),'container_names':sorted(c['Name'] for c in network.get('Containers',{}).values())},
 'expiry_timer':call(['systemctl','show','sova-expiry.timer','-p','NextElapseUSecRealtime']).decode().strip()}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "artifacts"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.artifacts.mkdir(mode=0o700, exist_ok=False)
    inputs = json.loads((args.inputs / "identity.json").read_text())
    links = json.loads(args.connections.read_text())
    if inputs["run_id"] != "b0be270e62" or datetime.now(UTC) >= datetime.fromisoformat(
        inputs["expires_at"]
    ):
        raise ValueError("Unexpired owned canary required")
    verify_vm()
    config = Config(
        connect_timeout=5,
        read_timeout=10,
        retries={"max_attempts": 1, "mode": "standard"},
    )
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    caller = session.client("sts", config=config).get_caller_identity()
    if caller["Account"] != "265233844540":
        raise ValueError("Personal account only")
    ec2 = session.client("ec2", config=config)
    instance = ec2.describe_instances(InstanceIds=["i-04946f0e0c0548c3b"])[
        "Reservations"
    ][0]["Instances"][0]
    if {tag["Key"]: tag["Value"] for tag in instance["Tags"]}.get("RunId") != inputs[
        "run_id"
    ] or instance["State"]["Name"] != "running":
        raise ValueError("Owned source not running")
    groups = ec2.describe_security_groups(
        GroupIds=[group["GroupId"] for group in instance["SecurityGroups"]]
    )["SecurityGroups"]
    asg = session.client("autoscaling", config=config).describe_auto_scaling_instances(
        InstanceIds=[instance["InstanceId"]]
    )
    ssm = session.client("ssm", config=config)
    command = (
        "python3 -c \"import base64;exec(base64.b64decode('"
        + base64.b64encode(SOURCE_PROJECTION.encode()).decode()
        + "'))\""
    )
    receipt = ssm.send_command(
        InstanceIds=[instance["InstanceId"]],
        DocumentName="AWS-RunShellScript",
        Comment="Read projected owned Sova dependency metadata only",
        Parameters={"commands": [command], "executionTimeout": ["90"]},
    )
    command_id = receipt["Command"]["CommandId"]
    (args.artifacts / "source-command.json").write_text(
        json.dumps(
            {
                "command_id": command_id,
                "instance_id": instance["InstanceId"],
                "read_only_projection_sha256": hashlib.sha256(
                    SOURCE_PROJECTION.encode()
                ).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    deadline = time.monotonic() + 105
    while time.monotonic() < deadline:
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance["InstanceId"]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvocationDoesNotExist":
                raise
            time.sleep(0.5)
            continue
        if result["Status"] in {"Success", "Failed", "Cancelled", "TimedOut"}:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Read-only source metadata timed out")
    if result["Status"] != "Success":
        (args.artifacts / "source-diagnostic.txt").write_text(
            result.get("StandardErrorContent", "")
        )
        raise RuntimeError("Source projection failed; inspect private diagnostic")
    source = json.loads(result["StandardOutputContent"])
    namespace = json.loads(
        guest(
            KUBECTL,
            "--kubeconfig",
            CONFIG,
            "get",
            "ns",
            inputs["namespace"],
            "-o",
            "json",
        )
    )
    if namespace["metadata"]["uid"] != "565b91c4-4f6e-4d06-94c6-9f8803afa6c9":
        raise ValueError("Destination namespace replaced")
    pods = json.loads(
        guest(
            KUBECTL,
            "--kubeconfig",
            CONFIG,
            "-n",
            inputs["namespace"],
            "get",
            "pods",
            "-l",
            "sova.run=b0be270e62",
            "-o",
            "json",
        )
    )
    projected = []
    for pod in pods["items"]:
        projected.append(
            {
                "name": pod["metadata"]["name"],
                "uid": pod["metadata"]["uid"],
                "owner_references": pod["metadata"].get("ownerReferences", []),
                "containers": [
                    {
                        "name": c["name"],
                        "image": c["image"],
                        "security_context": c.get("securityContext", {}),
                    }
                    for c in pod["spec"]["containers"]
                ],
                "runtime": [
                    {
                        key: c.get(key)
                        for key in (
                            "name",
                            "imageID",
                            "containerID",
                            "ready",
                            "restartCount",
                        )
                    }
                    for c in pod.get("status", {}).get("containerStatuses", [])
                ],
            }
        )
    databases = {}
    for side in ("source", "target"):
        entry = inputs["environments"][side]
        dsn = make_conninfo(
            host=entry["database_host"],
            hostaddr="127.0.0.1",
            port=links["ports"][side + "_db"],
            user="postgres",
            password=(args.inputs / ("client/" + side + "-admin.password")).read_text(),
            dbname=entry["database"],
            sslmode="verify-full",
            sslrootcert=str(args.inputs / "pki/ca.pem"),
            connect_timeout=5,
        )
        with psycopg.connect(dsn) as conn:
            system, version, oid, prepared = conn.execute(
                "SELECT (SELECT system_identifier::text FROM pg_control_system()),"
                "current_setting('server_version'),(SELECT oid::text FROM pg_database WHERE datname=current_database()),current_setting('max_prepared_transactions')"
            ).fetchone()
            roles = conn.execute(
                "SELECT oid::text,rolname,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls "
                "FROM pg_roles ORDER BY rolname"
            ).fetchall()
            effective = []
            for role in roles:
                for table in ("orders", "work_items", "idempotency_results"):
                    privs = conn.execute(
                        "SELECT has_table_privilege(%s,%s,'INSERT'),has_table_privilege(%s,%s,'UPDATE'),"
                        "has_table_privilege(%s,%s,'DELETE'),has_table_privilege(%s,%s,'TRUNCATE')",
                        (role[1], table) * 4,
                    ).fetchone()
                    if any(privs):
                        effective.append(
                            {
                                "role": role[1],
                                "role_oid": role[0],
                                "table": table,
                                "write_privileges": list(privs),
                                "login": role[2],
                            }
                        )
            members = conn.execute(
                "SELECT member.rolname,parent.rolname,m.admin_option FROM pg_auth_members m "
                "JOIN pg_roles member ON member.oid=m.member JOIN pg_roles parent ON parent.oid=m.roleid ORDER BY 1,2"
            ).fetchall()
            tables = conn.execute(
                "SELECT c.relname,c.oid::text,owner.rolname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_roles owner ON owner.oid=c.relowner WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY 1"
            ).fetchall()
            tls = conn.execute(
                "SELECT ssl,version,cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
            ).fetchone()
        databases[side] = {
            "system_identifier": system,
            "version": version,
            "database_oid": oid,
            "database": entry["database"],
            "max_prepared_transactions": int(prepared),
            "roles": [
                dict(
                    zip(
                        (
                            "oid",
                            "name",
                            "login",
                            "superuser",
                            "create_role",
                            "create_db",
                            "replication",
                            "bypass_rls",
                        ),
                        row,
                    )
                )
                for row in roles
            ],
            "effective_writers": effective,
            "memberships": members,
            "tables": tables,
            "tls": list(tls),
        }
    report = {
        "version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "run_id": inputs["run_id"],
        "source": source,
        "aws": {
            "account": caller["Account"],
            "instance_id": instance["InstanceId"],
            "ami": instance["ImageId"],
            "architecture": instance["Architecture"],
            "security_group_inbound_rules": sum(
                len(group["IpPermissions"]) for group in groups
            ),
            "autoscaling_memberships": len(asg["AutoScalingInstances"]),
        },
        "target": {"namespace_uid": namespace["metadata"]["uid"], "pods": projected},
        "databases": databases,
        "writer_inventory_complete": False,
        "control_inventory_complete": False,
        "signed": False,
        "business_data_changed": False,
    }
    (args.artifacts / "facts.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "source_containers": len(source["containers"]),
                "target_pods": len(projected),
                "sql_servers": len(databases),
                "completeness_claimed": False,
                "signed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
