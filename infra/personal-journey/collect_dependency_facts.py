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
def call(args):
 p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
 if p.returncode:
  raise RuntimeError('Read-only projection command failed: '+p.stderr.decode(errors='replace')[:2000])
 return p.stdout
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
files=['qualification/order_app.py','qualification/order_runtime.py','qualification/requests.py','service/http_boundary.py']
print(json.dumps({'tenant':c.tenant,'application':c.application,'run':c.run,'side':c.side,'role':c.role,
'database':c.database,'image_digest':c.image_digest,'expires_at':c.expires_at.isoformat(),
'source_hashes':{f:hashlib.sha256((pathlib.Path('/app/sova')/f).read_bytes()).hexdigest() for f in files}}))"""
app=json.loads(call(base+['exec','--user','10001:10001','orders-app','python','-c',program]))
network=json.loads(call(base+['network','inspect','sova-b0be270e62']))[0]
units=call(['systemctl','list-units','--all','--no-legend','--plain','sova-*','containerd.service','amazon-ssm-agent.service']).decode().splitlines()
if len(units)>64:raise ValueError('Owned controller inventory exceeds qualified bound')
controllers=[]
for line in units:
 name=line.split()[0]
 if not __import__('re').fullmatch(r'(?:sova-[A-Za-z0-9_.@-]+|containerd|amazon-ssm-agent)\.(?:service|timer)',name):
  raise ValueError('Unexpected source controller identifier')
 properties=['Id','LoadState','ActiveState','SubState','UnitFileState','Restart','TriggeredBy','Triggers','FragmentPath']
 argv=['systemctl','show',name]
 for prop in properties:argv+=['-p',prop]
 values=dict(part.split('=',1) for part in call(argv).decode().splitlines() if '=' in part)
 fragment=values.get('FragmentPath');digest=None
 if fragment:
  path=pathlib.Path(fragment).resolve(strict=True);info=path.stat()
  ordinary=any(path.is_relative_to(root) for root in (pathlib.Path('/etc/systemd/system'),pathlib.Path('/usr/lib/systemd/system')))
  owned_transient=(path.parent==pathlib.Path('/run/systemd/transient') and path.name==name
      and __import__('re').fullmatch(r'sova-wg-b0be-expiry-[0-9]{10}\.(?:service|timer)',name))
  if (not (ordinary or owned_transient)
      or not __import__('stat').S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_size>65536):
   raise ValueError('Controller fragment outside the read-only inventory boundary')
  digest=hashlib.sha256(path.read_bytes()).hexdigest()
 controllers.append({'properties':values,'fragment_sha256':digest})
payload=json.dumps({'containers':containers,'application_projection':app,
 'network':{'id':network.get('Id'),'name':network.get('Name'),'internal':network.get('Internal'),
 'ipam':network.get('IPAM'),'container_names':sorted(c['Name'] for c in network.get('Containers',{}).values())},
 'controllers':controllers,'controller_behavior_or_external_authority_closure_proved':False,
 'expiry_timer':call(['systemctl','show','sova-expiry.timer','-p','NextElapseUSecRealtime']).decode().strip()})
if len(payload.encode())>22000:raise ValueError('Source projection exceeds qualified SSM byte bound')
print(payload)
'''


def main():
    collector_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
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
    # These calls stay scoped to the one owned source. Never list unrelated
    # account-wide schedules, sessions, IAM users, or resource command bodies.
    association_status = ssm.describe_instance_associations_status(
        InstanceId=instance["InstanceId"], MaxResults=50
    )
    if association_status.get("NextToken"):
        raise ValueError(
            "Source association status is incomplete; never truncate as complete"
        )
    associations = [
        {
            key: item.get(key)
            for key in (
                "AssociationId",
                "Name",
                "AssociationVersion",
                "Status",
                "DetailedStatus",
            )
        }
        for item in association_status.get("InstanceAssociationStatusInfos", [])
    ]
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
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            conn.execute("SET LOCAL lock_timeout = '1s'")
            (
                system,
                version,
                oid,
                prepared,
                read_only,
                isolation,
                timeout,
                lock_timeout,
            ) = conn.execute(
                "SELECT (SELECT system_identifier::text FROM pg_control_system()),"
                "current_setting('server_version'),(SELECT oid::text FROM pg_database WHERE datname=current_database()),"
                "current_setting('max_prepared_transactions'),current_setting('transaction_read_only'),"
                "current_setting('transaction_isolation'),current_setting('statement_timeout'),current_setting('lock_timeout')"
            ).fetchone()
            if (read_only, isolation, timeout, lock_timeout) != (
                "on",
                "repeatable read",
                "5s",
                "1s",
            ):
                raise ValueError("Actual SQL collection is not read-only and bounded")
            if (
                system
                != {"source": "7709720208587968539", "target": "7709723763091251229"}[
                    side
                ]
            ):
                raise ValueError(
                    "Database server identity differs before catalog collection"
                )
            roles = conn.execute(
                "SELECT oid::text,rolname,rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls "
                "FROM pg_roles ORDER BY rolname LIMIT 257"
            ).fetchall()
            if len(roles) > 256:
                raise ValueError(
                    "Role inventory exceeds the qualified collection bound"
                )
            # SET ROLE can expose a writer even when ordinary inherited
            # privileges are absent. Observe the actual reachability relation.
            assumptions = conn.execute(
                "SELECT a.rolname,b.rolname FROM pg_roles a CROSS JOIN pg_roles b "
                "WHERE a.rolcanlogin AND a.oid<>b.oid AND pg_has_role(a.oid,b.oid,'SET') "
                "ORDER BY 1,2 LIMIT 65537"
            ).fetchall()
            definers = conn.execute(
                "SELECT n.nspname,p.proname,p.oid::text,r.rolname,"
                "encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex') "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "JOIN pg_roles r ON r.oid=p.proowner WHERE p.prosecdef "
                "AND n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2,3 LIMIT 257"
            ).fetchall()
            definer_callers = conn.execute(
                "SELECT a.rolname,p.oid::text FROM pg_roles a CROSS JOIN pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE a.rolcanlogin AND p.prosecdef "
                "AND n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND has_function_privilege(a.oid,p.oid,'EXECUTE') ORDER BY 1,2 LIMIT 65537"
            ).fetchall()
            extensions = conn.execute(
                "SELECT extname,extversion FROM pg_extension ORDER BY 1 LIMIT 257"
            ).fetchall()
            event_triggers = conn.execute(
                "SELECT evtname,evtevent,evtenabled FROM pg_event_trigger ORDER BY 1 LIMIT 257"
            ).fetchall()
            foreign_tables = conn.execute(
                "SELECT n.nspname,c.relname FROM pg_foreign_table f JOIN pg_class c ON c.oid=f.ftrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace ORDER BY 1,2 LIMIT 257"
            ).fetchall()
            # One bounded catalog query avoids a round trip for every role/table.
            # ACL potential is not proof of successful login, RLS bypass, or a
            # complete inventory of external clients possessing credentials.
            writer_rows = conn.execute(
                "SELECT r.oid::text,r.rolname,c.relname,r.rolcanlogin,"
                "has_table_privilege(r.oid,c.oid,'INSERT'),has_table_privilege(r.oid,c.oid,'UPDATE'),"
                "has_table_privilege(r.oid,c.oid,'DELETE'),has_table_privilege(r.oid,c.oid,'TRUNCATE') "
                "FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relname=ANY(%s) "
                "ORDER BY r.rolname,c.relname LIMIT 769",
                (["orders", "work_items", "idempotency_results"],),
            ).fetchall()
            effective = [
                {
                    "role": r[1],
                    "role_oid": r[0],
                    "table": r[2],
                    "write_privileges": list(r[4:]),
                    "login": r[3],
                }
                for r in writer_rows
                if any(r[4:])
            ]
            sequence_access = conn.execute(
                "SELECT r.rolname,c.relname,r.rolcanlogin,"
                "has_sequence_privilege(r.oid,c.oid,'USAGE'),has_sequence_privilege(r.oid,c.oid,'UPDATE') "
                "FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind='S' "
                "AND (has_sequence_privilege(r.oid,c.oid,'USAGE') OR has_sequence_privilege(r.oid,c.oid,'UPDATE')) "
                "ORDER BY r.rolname,c.relname LIMIT 4097"
            ).fetchall()
            database_access = conn.execute(
                "SELECT rolname,rolcanlogin,has_database_privilege(oid,current_database(),'CONNECT'),"
                "has_database_privilege(oid,current_database(),'CREATE') FROM pg_roles ORDER BY rolname LIMIT 257"
            ).fetchall()
            members = conn.execute(
                "SELECT member.rolname,parent.rolname,m.admin_option,m.inherit_option,m.set_option FROM pg_auth_members m "
                "JOIN pg_roles member ON member.oid=m.member JOIN pg_roles parent ON parent.oid=m.roleid ORDER BY 1,2 LIMIT 8193"
            ).fetchall()
            tables = conn.execute(
                "SELECT c.relname,c.oid::text,owner.rolname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_roles owner ON owner.oid=c.relowner WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY 1 LIMIT 257"
            ).fetchall()
            bounded = [
                (assumptions, 65536),
                (definers, 256),
                (definer_callers, 65536),
                (extensions, 256),
                (event_triggers, 256),
                (foreign_tables, 256),
                (writer_rows, 768),
                (sequence_access, 4096),
                (database_access, 256),
                (members, 8192),
                (tables, 256),
            ]
            if any(len(rows) > limit for rows, limit in bounded):
                raise ValueError(
                    "Catalog collection exceeds the qualified bound; never truncate as complete"
                )
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
            "sequence_authorities": sequence_access,
            "database_access": database_access,
            "collection": {
                "read_only": True,
                "isolation": "repeatable_read",
                "actual_session_settings": {
                    "transaction_read_only": read_only,
                    "transaction_isolation": isolation,
                    "statement_timeout": timeout,
                    "lock_timeout": lock_timeout,
                },
                "statement_timeout_ms": 5000,
                "lock_timeout_ms": 1000,
                "function_hash_algorithm": "sha256",
                "privileges_are_potential_not_confirmed_writes": True,
                "external_clients_enumerated": False,
                "complete_temporal_change_feed": False,
            },
            "memberships": members,
            "login_role_assumptions": assumptions,
            "security_definer_functions": definers,
            "security_definer_callers": definer_callers,
            "extensions": extensions,
            "event_triggers": event_triggers,
            "foreign_tables": foreign_tables,
            "tables": tables,
            "tls": list(tls),
        }
    report = {
        "version": 1,
        "collection_protocol_version": 2,
        "collector_sha256": collector_sha256,
        "source_projection_sha256": hashlib.sha256(
            SOURCE_PROJECTION.encode()
        ).hexdigest(),
        "source_command_id": command_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "run_id": inputs["run_id"],
        "source": source,
        "source_ssm_associations": associations,
        "controller_inventory_scope": "Owned source systemd metadata and SSM association status only; not root/session/cron/IAM closure",
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
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != collector_sha256:
        raise ValueError(
            "Collector code changed during measurement; preserve diagnostics instead of signing"
        )
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
