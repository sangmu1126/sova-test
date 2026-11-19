#!/bin/bash
# New run-owned canary only. No migration, route switch or existing DB reset.
set -euo pipefail
umask 077
export PATH=/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin
run_id=${1:?run ID}; app_archive=${2:?archive SHA256}; app_manifest=${3:?manifest SHA256}
pg_archive=${4:?archive SHA256}; pg_manifest=${5:?manifest SHA256}
secret_version=${6:?pinned source Secret version}
[[ $run_id =~ ^[a-z0-9]{8,12}$ ]]
[[ $secret_version =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$ ]]
for digest in "$app_archive" "$app_manifest" "$pg_archive" "$pg_manifest"; do
  [[ $digest =~ ^[a-f0-9]{64}$ ]]
done
[[ $(id -u) == 0 && $(uname -m) == aarch64 ]]
[[ $(cat /var/lib/sova-journey/run-id) == "$run_id" ]]
systemctl is-active --quiet sova-expiry.timer
[[ $(aws sts get-caller-identity --region ap-northeast-2 --query Account --output text) == 265233844540 ]]
state=/var/lib/sova-journey/workload
[[ ! -e $state ]]  # Never reset an earlier attempt or silently replay bootstrap.
mkdir -m 0700 "$state"
namespace=sova-$run_id
ctr namespaces create "$namespace"
aws secretsmanager get-secret-value --region ap-northeast-2 \
  --secret-id "sova/qualification/$run_id/source" --version-id "$secret_version" \
  --version-stage AWSCURRENT --query SecretString --output text > "$state/credentials.json"
python3 - "$state" "$run_id" "$app_manifest" <<'PY'
import base64,json,os,sys
from datetime import datetime,timezone,timedelta
from pathlib import Path
root=Path(sys.argv[1]);raw=(root/'credentials.json').read_bytes()
if len(raw)>65536:raise ValueError('Bounded source Secret required')
value=json.loads(raw)
expected={'db':{'ca.pem','server.pem','server-key.pem','admin-password','pg_hba.conf','init.sql'},
          'app':{'ca.pem','server.pem','server-key.pem','config.json','database.dsn','database-ca.pem','client.token'}}
if set(value)!={'version','run_id','files'} or value['version']!=1 or value['run_id']!=sys.argv[2] or set(value['files'])!=set(expected):
    raise ValueError('Source Secret scope mismatch')
config=json.loads(base64.b64decode(value['files']['app']['config.json'],validate=True))
expires=datetime.fromisoformat(config['expires_at']);now=datetime.now(timezone.utc)
if config['side']!='source' or config['run_id']!=sys.argv[2] or config['deployment_digest']!='sha256:'+sys.argv[3] or expires.tzinfo is None or not now<expires<=now+timedelta(hours=8):
    raise ValueError('Source workload binding or deadline mismatch')
for kind,names in expected.items():
    files=value['files'][kind]
    if set(files)!=names:raise ValueError('Unexpected mounted source files')
    directory=root/kind;directory.mkdir(mode=0o700)
    for name,encoded in files.items():
        content=base64.b64decode(encoded,validate=True)
        if not 0<len(content)<=16384:raise ValueError('Oversized source credential file')
        fd=os.open(directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(content)
PY
chown -R 70:70 "$state/db"
chown -R 10001:10001 "$state/app"
mkdir -m 0700 "$state/data"
chown 70:70 "$state/data"

for kind in app db; do
  if [[ $kind == app ]]; then archive=$app_archive; manifest=$app_manifest; else archive=$pg_archive; manifest=$pg_manifest; fi
  aws s3 cp "s3://sova-journey-${run_id}-265233844540/source/images/$archive/canary.oci.tar" \
    "$state/$kind.oci.tar" --region ap-northeast-2 --only-show-errors
  echo "$archive  $state/$kind.oci.tar" | sha256sum --check --status
  ctr --namespace "$namespace" images import --base-name "local.sova.invalid/$run_id/$kind" \
    --digests "$state/$kind.oci.tar"
  actual=$(ctr --namespace "$namespace" content get "sha256:$manifest" | sha256sum)
  [[ ${actual%% *} == "$manifest" ]]
done

nerdctl --namespace "$namespace" network create --internal --subnet 10.246.0.0/24 \
  --gateway 10.246.0.1 "$namespace"
nerdctl --namespace "$namespace" run -d --name orders-db --label "sova.run=$run_id" \
  --network "$namespace" --ip 10.246.0.10 --user 70:70 --read-only \
  --cap-drop ALL --security-opt no-new-privileges --memory 512m --cpus 1 --pids-limit 128 \
  --mount "type=bind,src=$state/db,dst=/run/sova-db,ro" \
  --mount "type=bind,src=$state/db/init.sql,dst=/docker-entrypoint-initdb.d/001-run.sql,ro" \
  --mount "type=bind,src=$state/data,dst=/var/lib/postgresql/data" \
  --tmpfs /var/run/postgresql:rw,noexec,nosuid,size=2m,uid=70,gid=70,mode=0700 \
  --tmpfs /tmp:rw,noexec,nosuid,size=4m,uid=70,gid=70,mode=0700 \
  --env POSTGRES_DB=source_orders --env POSTGRES_PASSWORD_FILE=/run/sova-db/admin-password \
  --env 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=peer --data-checksums' \
  "local.sova.invalid/$run_id/db@sha256:$pg_manifest" postgres -c ssl=on \
  -c ssl_cert_file=/run/sova-db/server.pem -c ssl_key_file=/run/sova-db/server-key.pem \
  -c hba_file=/run/sova-db/pg_hba.conf -c password_encryption=scram-sha-256 \
  -c max_connections=40 -c shared_buffers=64MB -c log_min_error_statement=panic
ready=0
for attempt in {1..45}; do
  if nerdctl --namespace "$namespace" exec orders-db pg_isready -h 127.0.0.1 -U postgres -d source_orders >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ $ready == 1 ]]
# BEGIN TLS APP SPEC
health_code=$(cat <<'PY'
import http.client,json,socket,ssl
from pathlib import Path
c=json.loads(Path('/run/sova/config.json').read_text())
host=c['side']+'-app.'+c['run_id']+'.sova.invalid'
context=ssl.create_default_context(cafile='/run/sova/ca.pem')
s=context.wrap_socket(socket.create_connection(('127.0.0.1',8080),timeout=2),server_hostname=host)
s.sendall(('GET /healthz HTTP/1.1\r\nHost: '+host+'\r\nConnection: close\r\n\r\n').encode())
r=http.client.HTTPResponse(s);r.begin()
assert r.status==200 and r.getheader('x-sova-deployment')==c['deployment_digest']
body=json.loads(r.read(2049));assert body=={'application_id':c['application_id'],'side':c['side']}
r.close();s.close()
PY
)
health_encoded=$(printf '%s' "$health_code" | base64 -w 0)
health_command="python -c \"import base64;exec(base64.b64decode('$health_encoded'))\""
nerdctl --namespace "$namespace" run -d --name orders-app --label "sova.run=$run_id" \
  --network "$namespace" --ip 10.246.0.20 --user 10001:10001 --read-only \
  --cap-drop ALL --security-opt no-new-privileges --memory 256m --cpus 1 --pids-limit 64 \
  --health-cmd "$health_command" --health-interval 10s --health-timeout 3s --health-retries 3 \
  --mount "type=bind,src=$state/app,dst=/run/sova,ro" \
  "local.sova.invalid/$run_id/app@sha256:$app_manifest" \
  python -m uvicorn sova.qualification.order_runtime:create_app --factory --host 0.0.0.0 --port 8080 \
  --no-access-log --no-proxy-headers --limit-concurrency 24 --backlog 64 --timeout-keep-alive 3 \
  --timeout-graceful-shutdown 8 --no-server-header \
  --ssl-certfile /run/sova/server.pem --ssl-keyfile /run/sova/server-key.pem
# END TLS APP SPEC
printf '%s\n' 'Source canary processes requested; independent TLS, SQL and digest readback still required'
