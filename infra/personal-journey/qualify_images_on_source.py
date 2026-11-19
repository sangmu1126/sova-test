"""Measure patched candidates in an isolated namespace on the owned AWS VM.

Uses standard SSM/containerd/PostgreSQL, fresh short-lived test credentials,
versioned private S3 input delivery and no container external network. This
does not deploy a patch or qualify a migration, a customer schema or an SLO.
"""

import argparse
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from prepare_canary import artifact
from sova.qualification.order_schema import SCHEMA

PROBE = """import json,ssl,socket,urllib.request,urllib.error
from pathlib import Path
import psycopg
root=Path('/run/sova');cfg=json.loads((root/'config.json').read_text())
headers={'authorization':'Bearer '+(root/'client.token').read_text(),'x-qualification-tenant':cfg['tenant_id'],'x-qualification-run':cfg['run_id'],'idempotency-key':'fresh-image-trial','content-type':'application/json'}
ctx=ssl.create_default_context(cafile=str(root/'ca.pem'))
def request(path='/orders',data=None,override=None):
 r=urllib.request.Request('https://localhost:8080'+path,data=data,headers={**headers,**(override or {})})
 try:
  with urllib.request.urlopen(r,context=ctx,timeout=3) as s:return s.status,json.load(s),{k.lower():v for k,v in s.headers.items()}
 except urllib.error.HTTPError as s:return s.code,json.load(s),{k.lower():v for k,v in s.headers.items()}
checks={}
health=request('/healthz')
checks['verified_https_identity']=health[0]==200 and health[1]=={'application_id':cfg['application_id'],'side':'source'} and health[2].get('x-sova-deployment')==cfg['deployment_digest']
checks['unauthorized_denied']=request(data=b'{}',override={'authorization':''})[0]==401
checks['other_tenant_denied']=request(override={'x-qualification-tenant':'other'})[0]==403
body=json.dumps({'customer':'fresh-image-trial','amount':'12.50'}).encode()
a=request(data=body);b=request(data=body);d=request('/orders?customer=fresh-image-trial&idempotency_key=fresh-image-trial')
checks['actual_order_idempotent_replay']=a[0]==b[0]==d[0]==200 and a[1]==b[1]==d[1]
dsn=(root/'database.dsn').read_text()
with psycopg.connect(dsn) as c:
 checks['one_committed_business_effect']=c.execute('SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)').fetchone()==(1,1,1)
 checks['database_tls']=c.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()==(True,)
 checks['no_admin_role']=c.execute('SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname=current_user').fetchone()==(False,)
def denied(reason,**kwargs):
 try:
  with psycopg.connect(dsn,**kwargs):return False
 except psycopg.OperationalError as e:
  text=str(e).lower()
  expected={'ca':lambda: 'certificate verify failed' in text,'host':lambda: 'does not match host name' in text or 'hostname mismatch' in text,'password':lambda: 'password authentication failed' in text,'plaintext':lambda: 'pg_hba.conf' in text and ('no encryption' in text or 'ssl off' in text),'database':lambda: 'permission denied for database' in text and 'other_tenant' in text}
  return expected[reason]()
checks['wrong_ca_denied']=denied('ca',sslrootcert=str(root/'untrusted-ca.pem'))
checks['wrong_hostname_denied']=denied('host',host='unregistered.invalid')
checks['wrong_password_denied']=denied('password',password='wrong-image-trial-password')
checks['plaintext_without_password_denied']=denied('plaintext',sslmode='disable',password='')
checks['other_database_denied']=denied('database',dbname='other_tenant')
s=socket.socket();s.settimeout(1)
checks['no_external_network']=s.connect_ex(('169.254.169.254',80))==101
s.close()
print(json.dumps(checks))
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "app-image", "postgres-image", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    identity = json.loads((a.inputs / "identity.json").read_text())
    now = datetime.now(UTC)
    end = min(
        datetime.fromisoformat(identity["expires_at"]), now + timedelta(minutes=10)
    )
    if identity["run_id"] != "b0be270e62" or end - now < timedelta(minutes=6):
        raise ValueError("Owned trial needs enough time for the bounded image check")
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    cfg = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1})
    if (
        session.client("sts", config=cfg).get_caller_identity()["Account"]
        != "265233844540"
    ):
        raise ValueError("Wrong personal account")
    ec2, ssm, s3 = (session.client(n, config=cfg) for n in ("ec2", "ssm", "s3"))
    instance = ec2.describe_instances(InstanceIds=["i-04946f0e0c0548c3b"])[
        "Reservations"
    ][0]["Instances"][0]
    if (
        instance["State"]["Name"] != "running"
        or {t["Key"]: t["Value"] for t in instance["Tags"]}.get("RunId") != "b0be270e62"
    ):
        raise ValueError("Wrong owned source")
    bucket = "sova-journey-b0be270e62-265233844540"
    if {
        t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    }.get("RunId") != "b0be270e62":
        raise ValueError("Wrong artifact bucket")
    if not all(
        s3.get_public_access_block(Bucket=bucket)[
            "PublicAccessBlockConfiguration"
        ].values()
    ):
        raise ValueError("Private artifact bucket protections required")
    if (
        s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"][
            "Rules"
        ][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        != "AES256"
    ):
        raise ValueError("Expected bucket encryption required")
    images = {}
    for kind, folder in (("app", a.app_image), ("db", a.postgres_image)):
        proof = artifact(folder)
        key = "source/images/" + proof["archive_sha256"] + "/canary.oci.tar"
        try:
            existing = s3.head_object(Bucket=bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
                raise
            existing = None
        if existing is None:
            s3.upload_file(
                str(folder / "canary.oci.tar"),
                bucket,
                key,
                ExtraArgs={"Metadata": {"sha256": proof["archive_sha256"]}},
            )
        elif existing["Metadata"].get("sha256") != proof["archive_sha256"]:
            raise ValueError("Existing content-addressed image differs")
        images[kind] = {**proof, "key": key}
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from qualification_postgres import certificates

    cert = a.artifacts / "certificates"
    cert.mkdir(mode=0o700)
    certificates(cert, valid_until=end)
    untrusted = a.artifacts / "untrusted"
    untrusted.mkdir(mode=0o700)
    certificates(untrusted, valid_until=end)
    password, token = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    suffix = uuid4().hex[:12]
    run = "image-" + suffix
    db = {
        name: (cert / "server" / name).read_text()
        for name in ("server.pem", "server-key.pem", "ca.pem")
    }
    db.update(
        {
            "admin-password": secrets.token_urlsafe(40),
            "pg_hba.conf": "local all all trust\nhostnossl all all 0.0.0.0/0 reject\nhostssl all all 0.0.0.0/0 scram-sha-256\n",
        }
    )
    app = {
        **{
            name: (cert / "server" / name).read_text()
            for name in ("server.pem", "server-key.pem", "ca.pem")
        },
        "database-ca.pem": (cert / "ca.pem").read_text(),
        "untrusted-ca.pem": (untrusted / "ca.pem").read_text(),
        "database.dsn": f"host=localhost hostaddr=127.0.0.1 port=5432 dbname=orders_source user=orders_app password={password} sslmode=verify-full sslrootcert=/run/sova/database-ca.pem connect_timeout=2",
        "client.token": token,
        "config.json": json.dumps(
            {
                "version": 1,
                "tenant_id": "tenant-image-trial",
                "application_id": "orders-image-trial",
                "run_id": run,
                "side": "source",
                "deployment_digest": images["app"]["manifest_digest"],
                "expires_at": end.isoformat(),
            }
        ),
    }
    sql = (
        "CREATE ROLE orders_app LOGIN PASSWORD '"
        + password
        + "';\nCREATE DATABASE other_tenant;REVOKE CONNECT ON DATABASE other_tenant FROM PUBLIC;\n"
        + SCHEMA
        + "\nGRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO orders_app;\nGRANT USAGE,SELECT ON SEQUENCE order_number_seq TO orders_app;\n"
    )
    raw = json.dumps({"app": app, "db": db, "role_sql": sql}).encode()
    key = "source/image-trials/" + suffix + "/inputs.json"
    uploaded = s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=raw,
        ServerSideEncryption="AES256",
        ContentType="application/json",
    )
    version = uploaded["VersionId"]
    code = Path(__file__).with_name("source_image_trial.py").read_text()
    payload = {
        "suffix": suffix,
        "expires_at": end.isoformat(),
        "bucket": bucket,
        "input_key": key,
        "input_version": version,
        "input_etag": uploaded["ETag"],
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "images": images,
        "probe_program": PROBE,
    }
    script = (
        "import base64,io,sys;sys.stdin=io.StringIO(base64.b64decode('"
        + base64.b64encode(json.dumps(payload).encode()).decode()
        + "').decode());exec(base64.b64decode('"
        + base64.b64encode(code.encode()).decode()
        + "'))"
    )
    report = {
        "source_instance": "i-04946f0e0c0548c3b",
        "source_trial_code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "probe_sha256": hashlib.sha256(PROBE.encode()).hexdigest(),
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "input_s3_version": version,
        "input_s3_key": key,
        "candidate_images": images,
        "passed": False,
        "migration_complete": False,
        "input_object_removed": False,
    }
    command = ssm.send_command(
        InstanceIds=["i-04946f0e0c0548c3b"],
        DocumentName="AWS-RunShellScript",
        Comment="Owned isolated fresh-credential image runtime trial",
        Parameters={
            "commands": ['python3 -c "' + script + '"'],
            "executionTimeout": ["240"],
        },
    )["Command"]["CommandId"]
    report["command_id"] = command
    (a.artifacts / "request.json").write_text(json.dumps(report, indent=2) + "\n")
    until = time.monotonic() + 270
    while time.monotonic() < until:
        try:
            result = ssm.get_command_invocation(
                CommandId=command, InstanceId="i-04946f0e0c0548c3b"
            )
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(0.5)
            continue
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
        time.sleep(1)
    else:
        raise RuntimeError(
            "Source image trial unresolved; inspect command and expiry before any retry"
        )
    if result["Status"] in {"Success", "Failed"} and result.get("StandardOutputContent", "").strip():
        observed = json.loads(result["StandardOutputContent"])
        if observed.get("run_id") != identity["run_id"] or observed.get("trial_id") != "sova-image-" + suffix:
            raise ValueError("Source trial receipt belongs to another run")
        report.update(observed)
        report["passed"] = (
            result["Status"] == "Success"
            and observed.get("passed") is True
            and observed.get("original_unchanged") is True
            and observed.get("containers_removed") is True
        )
        if observed.get("original_unchanged") is True and observed.get("containers_removed") is True:
            s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
            report["input_object_removed"] = True
    if result["Status"] != "Success":
        (a.artifacts / "private-failure.txt").write_text(
            result.get("StandardErrorContent", "")[-6000:]
        )
        report["status"] = result["Status"]
    (a.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "checks": len(report.get("checks", {})),
                "original_unchanged": report.get("original_unchanged"),
                "input_object_removed": report["input_object_removed"],
            }
        )
    )
    if not report["passed"]:
        raise RuntimeError(
            "Isolated source trial did not qualify; private evidence retained"
        )


if __name__ == "__main__":
    main()
