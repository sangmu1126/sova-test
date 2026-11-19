"""Real pg_dump/pg_restore on the owned VM; no application route/write change.

Passwords travel on stdin to a private tmpfs pgpass file, never argv/env/logs.
The archive stays on the already verified LUKS volume. A fresh restore database
is retained with PUBLIC access revoked. This is a warm restore trial, not HA,
off-site disaster recovery, retention lock, or a complete backup qualification.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from import_target_images import VM, identity
from sova.adapters.postgres_digest import table_digest


REMOTE = r'''
import hashlib,json,os,pathlib,shutil,subprocess,sys
c=json.load(sys.stdin);os.umask(0o077)
def call(args,**kw):
 p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=150,**kw)
 if p.returncode:
  raise RuntimeError('Fixed backup client failed; exit='+str(p.returncode))
 return p.stdout
if pathlib.Path('/etc/machine-id').read_text().strip()!='a789f623bca040989f282881d9a21654':
 raise ValueError('Wrong destination VM')
mount=pathlib.Path('/var/lib/sova-qualification/user-journey-b0be270e62/mounted')
if (mount/'.sova-encrypted-volume').read_text().strip()!='b0be270e62':
 raise ValueError('Wrong retained volume marker')
found=json.loads(call(['findmnt','-J','-T',str(mount)]))['filesystems'][0]
if found['target']!=str(mount) or found['source']!='/dev/mapper/sova-b0be270e62':
 raise ValueError('Encrypted filesystem is not actually mounted')
tmp=pathlib.Path('/run')/('sova-backup-'+c['suffix'])
if json.loads(call(['findmnt','-J','-T','/run']))['filesystems'][0]['fstype']!='tmpfs':
 raise ValueError('Credential staging must be tmpfs')
tmp.mkdir(mode=0o700);os.chown(tmp,70,70)
out=mount/'backups'/c['suffix'];out.mkdir(parents=True,mode=0o700,exist_ok=False);os.chown(out,70,70)
ctr=['/var/lib/rancher/rke2/bin/ctr','--address','/run/k3s/containerd/containerd.sock','-n','k8s.io']
image='local.sova.invalid/b0be270e62/db@sha256:01f80835db480f8403d638051eff0ace599264a908bad70a214b8952173363c5'
common=ctr+['run','--rm','--net-host','--read-only','--user','70:70',
 '--mount','type=bind,src='+str(tmp)+',dst=/input,options=rbind:ro',
 '--mount','type=bind,src='+str(out)+',dst=/output,options=rbind:rw',
 '--env','PGPASSFILE=/input/pgpass','--env','PGSSLMODE=verify-full',
 '--env','PGSSLROOTCERT=/input/ca.pem','--env','PGCONNECT_TIMEOUT=5',
 '--env','PGOPTIONS=-c statement_timeout=90000 -c lock_timeout=2000']
def escaped(s):
 if any(x in s for x in ('\n','\r','\x00')):raise ValueError('Unsupported pgpass field')
 return s.replace('\\','\\\\').replace(':','\\:')
try:
 for name,value in [('ca.pem',c['ca']),('pgpass','\n'.join(':'.join(map(escaped,[x['host'],str(x['port']),x['database'],'postgres',x['password']]))for x in c['connections'])+'\n')]:
  p=tmp/name;p.write_text(value);p.chmod(0o600);os.chown(p,70,70)
 source,target=c['connections']
 call(common+['--env','PGHOSTADDR=192.168.5.2',image,'sova-dump-'+c['suffix'],
  'pg_dump','--host',source['host'],'--port',str(source['port']),'--username','postgres',
  '--dbname',source['database'],'--format=custom','--no-owner','--no-privileges','--file=/output/source.dump'])
 archive=out/'source.dump'
 listing=call(common+[image,'sova-list-'+c['suffix'],'pg_restore','--list','/output/source.dump'])
 call(common+['--env','PGHOSTADDR=10.43.7.150',image,'sova-restore-'+c['suffix'],
  'pg_restore','--host',target['host'],'--port',str(target['port']),'--username','postgres',
  '--dbname',target['database'],'--no-owner','--no-privileges','--single-transaction',
  '--exit-on-error','/output/source.dump'])
 result={'archive':str(archive),'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
  'archive_bytes':archive.stat().st_size,'archive_list_sha256':hashlib.sha256(listing).hexdigest(),
  'client_image_digest':image.split('@')[1],'mounted_mapper':found['source'],
  'credential_staging':'VM tmpfs only; removed after command','restored':True}
 print(json.dumps(result))
finally:
 shutil.rmtree(tmp)
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "artifacts"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.artifacts.mkdir(mode=0o700, exist_ok=False)
    inputs = json.loads((a.inputs / "identity.json").read_text())
    links = json.loads(a.connections.read_text())
    if inputs["run_id"] != "b0be270e62" or datetime.now(UTC) >= datetime.fromisoformat(inputs["expires_at"]):
        raise ValueError("Unexpired personal run required")
    identity()
    suffix = uuid4().hex[:12]
    database = "q_backup_" + suffix

    def connect(side, db=None, *, autocommit=False):
        return psycopg.connect(make_conninfo(
            host=inputs["environments"][side]["database_host"], hostaddr="127.0.0.1",
            port=links["ports"][side + "_db"], dbname=db or inputs["environments"][side]["database"],
            user="postgres", password=(a.inputs / ("client/" + side + "-admin.password")).read_text(),
            sslmode="verify-full", sslrootcert=str(a.inputs / "pki/ca.pem"), connect_timeout=5,
            options="-c statement_timeout=15000 -c lock_timeout=2000"), autocommit=autocommit)

    def content(side, db=None):
        with connect(side, db) as conn:
            return {"tables": {t: table_digest(conn, t) for t in ("orders", "work_items", "idempotency_results")},
                    "sequences": conn.execute("SELECT sequencename,last_value FROM pg_sequences WHERE schemaname='public' ORDER BY 1").fetchall()}

    report = {"version": 1, "run_id": inputs["run_id"], "started_at": datetime.now(UTC).isoformat(),
              "scope": "actual source backup to encrypted VM and fresh isolated restore",
              "application_db_or_route_changed": False, "disaster_recovery_qualified": False,
              "passed": False, "restore_database": database, "checks": {}}
    source_before, target_before = content("source"), content("target")
    for side, expected in (("source", "7709720208587968539"), ("target", "7709723763091251229")):
        with connect(side) as conn:
            if conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0] != expected:
                raise ValueError("Registered server replaced")
    with connect("target", autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
        conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC,orders_app,orders_batch").format(sql.Identifier(database)))
    (a.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    payload = {"suffix": suffix, "ca": (a.inputs / "pki/ca.pem").read_text(), "connections": [
        {"host": inputs["environments"][side]["database_host"],
         "port": links["ports"]["source_db"] if side == "source" else 5432,
         "database": inputs["environments"][side]["database"] if side == "source" else database,
         "password": (a.inputs / ("client/" + side + "-admin.password")).read_text()}
        for side in ("source", "target")]}
    remote = subprocess.run(["limactl", "shell", "--workdir=/", VM, "--", "sudo", "-n", "python3", "-c", REMOTE],
        input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=420)
    if remote.returncode:
        # No secret-bearing payload or full child environment is logged.
        (a.artifacts / "remote-failure.txt").write_bytes(remote.stderr[-3000:])
        raise RuntimeError("Backup/restore failed; private bounded diagnostic retained")
    report["archive"] = json.loads(remote.stdout)
    restored = content("target", database)
    source_after, target_after = content("source"), content("target")
    with connect("target", database) as conn:
        public = conn.execute("SELECT has_database_privilege('orders_app',current_database(),'CONNECT'),"
                              "has_database_privilege('orders_batch',current_database(),'CONNECT')").fetchone()
    report["checks"] = {"restored_content_and_sequences_equal": restored == source_before,
                        "source_app_unchanged": source_before == source_after,
                        "target_app_unchanged": target_before == target_after,
                        "application_roles_cannot_connect_to_restore": public == (False, False)}
    report["source_content"] = source_before
    report["restored_content"] = restored
    report["passed"] = all(report["checks"].values())
    report["completed_at"] = datetime.now(UTC).isoformat()
    report["driver_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (a.artifacts / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        raise RuntimeError("Actual restore did not meet all checks")
    print(json.dumps({"passed": True, "checks": report["checks"], "dr_qualified": False}))


if __name__ == "__main__":
    main()
