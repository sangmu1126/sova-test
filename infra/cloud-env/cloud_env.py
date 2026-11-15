#!/usr/bin/env python3
"""One switch for the cloud test environments: AWS and GCP on and off together.

    cloud-env.sh configure --aws-profile P --aws-region R --gcp-project P --gcp-region R
    cloud-env.sh up [--hours 8] [--clouds aws,gcp]   create this run's base resources
    cloud-env.sh status                              lease, time left, what exists now
    cloud-env.sh env                                 eval "$(cloud-env.sh env)" for the tests
    cloud-env.sh extend --hours 2                    push the end of the lease back
    cloud-env.sh down                                delete everything of this run, verify
    cloud-env.sh expire                              what the expiry agent runs

A run is a lease: a short run id and an end time. Every resource the switch or
the live tests create carries ``sova-test`` and the run id as a separate token
in its name (and a ``sova-test`` tag or label where the service has them).
``down`` lists what exists in each cloud, deletes exactly the resources of
this run, lists again and ends the lease only when nothing is left. Anything
it cannot delete yet (an object under compliance retention) keeps the lease
open, and the next ``down`` or expiry run tries again.

The lease ends on its own, twice over:
  * a launchd agent on this machine runs ``expire`` every ten minutes; after
    the end time it runs ``down`` (a missed run happens at the next wake);
  * a scheduled deletion in each cloud removes the run's database instance,
    the only resource with a noticeable hourly price, 30 minutes after the end
    time, even if this machine is asleep or gone. ``extend`` moves both.

Secrets (database passwords) are generated here, kept in 0600 files under the
state directory and never printed or put on a command line: the instances get
them through request files and API bodies, the tests through a passfile and
``$(cat file)`` in the printed environment. Only the standard library is used,
with the ``aws`` and ``gcloud`` command lines.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

AGENT = "com.sova-test.cloud-env-expiry"
BACKSTOP_GRACE = timedelta(minutes=30)
MAX_HOURS = 24
RUN_ID = re.compile(r"[a-z0-9]{6}")
RDS_CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
IP_URL = "https://checkip.amazonaws.com"


class CloudEnvError(RuntimeError):
    pass


def now():
    return datetime.now(UTC).replace(microsecond=0)


def iso(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def scoped(name, run):
    """True only for a name this run owns: ``sova-test`` first, the run id as
    its own token (``sova-test-src-<run>-x``, ``sova-test/<run>/x``,
    ``sova-test-<run>-x.com.``). Everything else is never touched."""
    if not RUN_ID.fullmatch(run) or not name.startswith("sova-test"):
        return False
    return run in re.split(r"[-/._:@]", name)


def run_command(args, *, input=None, check=True, timeout=3600):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        lines = (result.stderr or result.stdout).strip().splitlines()
        errors = [line for line in lines if "ERROR" in line or "error occurred" in line]
        raise CloudEnvError(f"{' '.join(args[:4])} failed: {' | '.join((errors or lines)[-3:])}")
    return result


def _json(result):
    text = (result.stdout or "").strip()
    return json.loads(text) if text else {}


class Paths:
    def __init__(self, root):
        self.root = Path(root)
        self.config = self.root / "config.json"
        self.lease = self.root / "lease.json"
        self.secrets = self.root / "secrets"
        self.ca = self.root / "ca"
        self.history = self.root / "history"
        self.log = self.root / "expiry.log"
        self.pgpass = self.secrets / "pgpass"

    def ensure(self):
        for directory in (self.root, self.secrets, self.ca, self.history):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)

    def secret(self, name):
        return self.secrets / name

    def new_secret(self, name):
        path = self.secret(name)
        if not path.exists():
            write_private(path, "Sv1" + secrets.token_urlsafe(24) + "\n")
        return path

    def read_secret(self, name):
        return self.secret(name).read_text().strip()


def write_private(path, text):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    path.chmod(0o600)


def load_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    write_private(tmp, json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def http_json(method, url, token, project, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "x-goog-user-project": project,
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise CloudEnvError(f"{method} {url.split('?')[0]} -> {error.code}: {detail}") from None
    return json.loads(text) if text.strip() else {}


# ---------------------------------------------------------------- AWS


class Aws:
    name = "aws"

    def __init__(self, config, *, run=run_command, paths=None):
        self.profile, self.region = config["profile"], config["region"]
        self.engine = config.get("engine_version", "17.11")
        self.run, self.paths = run, paths

    def cli(self, service, operation, *args, check=True):
        result = self.run(["aws", service, operation, *args, "--profile", self.profile,
                           "--region", self.region, "--output", "json"], check=check)
        if result.returncode:
            return None
        return _json(result)

    def account(self):
        return self.cli("sts", "get-caller-identity")["Account"]

    @staticmethod
    def rds_id(run):
        return f"sova-test-pg-{run}"

    # -- up

    def up(self, lease):
        run = lease["run"]
        tags = [{"Key": "sova-test", "Value": run}]
        vpc = self.cli("ec2", "describe-vpcs", "--filters",
                       "Name=isDefault,Values=true")["Vpcs"][0]["VpcId"]
        group = f"sova-test-rds-{run}"
        found = self.cli("ec2", "describe-security-groups", "--filters",
                         f"Name=group-name,Values={group}", f"Name=vpc-id,Values={vpc}")
        if found["SecurityGroups"]:
            sg = found["SecurityGroups"][0]["GroupId"]
        else:
            sg = self.cli("ec2", "create-security-group", "--group-name", group, "--vpc-id", vpc,
                          "--description", f"sova test database {run}, one /32 only",
                          "--tag-specifications", json.dumps([
                              {"ResourceType": "security-group", "Tags": tags}]))["GroupId"]
            self.cli("ec2", "authorize-security-group-ingress", "--group-id", sg,
                     "--protocol", "tcp", "--port", "5432", "--cidr", f"{lease['ip']}/32")
        instance = self.rds_id(run)
        master = "sova_admin"
        if not self._instance(instance):
            password = self.paths.read_secret(self.paths.new_secret("rds-master").name)
            request = self.paths.secret("rds-create.json")
            write_private(request, json.dumps({
                "DBInstanceIdentifier": instance, "Engine": "postgres",
                "EngineVersion": self.engine, "DBInstanceClass": "db.t4g.micro",
                "AllocatedStorage": 20, "StorageType": "gp3", "MasterUsername": master,
                "MasterUserPassword": password, "VpcSecurityGroupIds": [sg],
                "PubliclyAccessible": True, "BackupRetentionPeriod": 0,
                "DeletionProtection": False, "MultiAZ": False,
                "AutoMinorVersionUpgrade": False, "Tags": tags}))
            try:
                self.cli("rds", "create-db-instance", "--cli-input-json", f"file://{request}")
            finally:
                request.unlink(missing_ok=True)
        self.paths.new_secret("rds-role")
        self.run(["aws", "rds", "wait", "db-instance-available", "--db-instance-identifier",
                  instance, "--profile", self.profile, "--region", self.region])
        host = self._instance(instance)["Endpoint"]["Address"]
        bundle = self.paths.ca / "rds-global-bundle.pem"
        if not bundle.exists():
            with urllib.request.urlopen(RDS_CA_URL, timeout=30) as response:
                bundle.write_bytes(response.read())
        state = {"rds_id": instance, "host": host, "master": master, "security_group": sg,
                 "ca": str(bundle)}
        state["backstop"] = self.backstop(run, instance, parse(lease["expires_at"]))
        return state

    def _instance(self, instance):
        found = self.cli("rds", "describe-db-instances", "--db-instance-identifier", instance,
                         check=False)
        return found["DBInstances"][0] if found else None

    def backstop(self, run, instance, expires, *, update=False):
        """An EventBridge Scheduler one-time schedule that deletes the instance
        after the lease even if this machine never runs ``down``."""
        account = self.account()
        role = f"sova-test-expiry-{run}"
        if not update:
            trust = {"Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
            if self.cli("iam", "get-role", "--role-name", role, check=False) is None:
                self.cli("iam", "create-role", "--role-name", role, "--assume-role-policy-document",
                         json.dumps(trust), "--tags", json.dumps([{"Key": "sova-test", "Value": run}]),
                         "--description", "deletes this run's test database after its lease")
            self.cli("iam", "put-role-policy", "--role-name", role, "--policy-name", "delete-db",
                     "--policy-document", json.dumps({"Version": "2012-10-17", "Statement": [{
                         "Effect": "Allow", "Action": "rds:DeleteDBInstance",
                         "Resource": f"arn:aws:rds:{self.region}:{account}:db:{instance}"}]}))
        at = (expires + BACKSTOP_GRACE).strftime("%Y-%m-%dT%H:%M:%S")
        target = {"Arn": "arn:aws:scheduler:::aws-sdk:rds:deleteDBInstance",
                  "RoleArn": f"arn:aws:iam::{account}:role/{role}",
                  # SDK integrations take the SDK's member names (DbInstanceIdentifier).
                  "Input": json.dumps({"DbInstanceIdentifier": instance,
                                       "SkipFinalSnapshot": True,
                                       "DeleteAutomatedBackups": True}),
                  "RetryPolicy": {"MaximumRetryAttempts": 20,
                                  "MaximumEventAgeInSeconds": 86400}}
        args = ["--name", role, "--schedule-expression", f"at({at})",
                "--schedule-expression-timezone", "UTC",
                "--flexible-time-window", json.dumps({"Mode": "OFF"}),
                "--action-after-completion", "DELETE", "--target", json.dumps(target)]
        last = None
        for _ in range(12):  # a new role is assumable by the scheduler a little later
            result = self.run(["aws", "scheduler", "update-schedule" if update else "create-schedule",
                               *args, "--profile", self.profile, "--region", self.region,
                               "--output", "json"], check=False)
            if not result.returncode or "ConflictException" in result.stderr:
                return {"schedule": role, "at": at + "Z"}
            last = [line for line in result.stderr.splitlines() if "error" in line.lower()][-1:]
            time.sleep(5)
        raise CloudEnvError(f"AWS expiry schedule not created: {last}")

    def extend(self, lease, expires):
        state = lease["clouds"].get("aws") or {}
        if state.get("rds_id"):
            state["backstop"] = self.backstop(lease["run"], state["rds_id"], expires, update=True)

    # -- inventory and sweep

    def inventory(self, run):
        found = []
        for bucket in self.cli("s3api", "list-buckets").get("Buckets", []):
            if scoped(bucket["Name"], run):
                found.append(("s3-bucket", bucket["Name"]))
        for db in self.cli("rds", "describe-db-instances").get("DBInstances", []):
            if scoped(db["DBInstanceIdentifier"], run):
                found.append(("rds-instance", db["DBInstanceIdentifier"]))
        for snap in self.cli("rds", "describe-db-snapshots", "--snapshot-type", "manual").get(
                "DBSnapshots", []):
            if scoped(snap["DBSnapshotIdentifier"], run):
                found.append(("rds-snapshot", snap["DBSnapshotIdentifier"]))
        for group in self.cli("ec2", "describe-security-groups", "--filters",
                              f"Name=tag:sova-test,Values={run}").get("SecurityGroups", []):
            found.append(("security-group", group["GroupId"]))
        for role in self.cli("iam", "list-roles").get("Roles", []):
            if scoped(role["RoleName"], run):
                found.append(("iam-role", role["RoleName"]))
        for pool in self.cli("cognito-idp", "list-user-pools", "--max-results", "60").get(
                "UserPools", []):
            if scoped(pool["Name"], run):
                found.append(("cognito-pool", pool["Id"]))
        for secret in self.cli("secretsmanager", "list-secrets", "--filters",
                               f"Key=name,Values=sova-test/{run}/").get("SecretList", []):
            if scoped(secret["Name"], run):
                found.append(("secret", secret["ARN"]))
        for cluster in self.cli("ecs", "list-clusters").get("clusterArns", []):
            if scoped(cluster.rsplit("/", 1)[-1], run):
                found.append(("ecs-cluster", cluster))
        for definition in self.cli("ecs", "list-task-definitions", "--family-prefix",
                                   f"sova-test-app-{run}").get("taskDefinitionArns", []):
            found.append(("ecs-task-definition", definition))
        for zone in self.cli("route53", "list-hosted-zones").get("HostedZones", []):
            if scoped(zone["Name"], run):
                found.append(("route53-zone", zone["Id"].rsplit("/", 1)[-1]))
        for schedule in self.cli("scheduler", "list-schedules", "--name-prefix",
                                 f"sova-test-expiry-{run}").get("Schedules", []):
            if scoped(schedule["Name"], run):
                found.append(("schedule", schedule["Name"]))
        return found

    def sweep(self, run, log):
        """Delete what ``inventory`` found, dependents first; return what is left."""
        found = self.inventory(run)
        kinds = {}
        for kind, ident in found:
            kinds.setdefault(kind, []).append(ident)
        for name in kinds.get("schedule", []):
            self.cli("scheduler", "delete-schedule", "--name", name, check=False)
            log(f"aws schedule deleted: {name}")
        for cluster in kinds.get("ecs-cluster", []):
            services = self.cli("ecs", "list-services", "--cluster", cluster)["serviceArns"]
            for service in services:
                self.cli("ecs", "update-service", "--cluster", cluster, "--service", service,
                         "--desired-count", "0", check=False)
                self.cli("ecs", "delete-service", "--cluster", cluster, "--service", service,
                         "--force", check=False)
            if services:
                self.run(["aws", "ecs", "wait", "services-inactive", "--cluster", cluster,
                          "--services", *services, "--profile", self.profile,
                          "--region", self.region], check=False)
            self.cli("ecs", "delete-cluster", "--cluster", cluster, check=False)
            log(f"aws ecs cluster deleted: {cluster.rsplit('/', 1)[-1]}")
        for definition in kinds.get("ecs-task-definition", []):
            self.cli("ecs", "deregister-task-definition", "--task-definition", definition,
                     check=False)
            self.cli("ecs", "delete-task-definitions", "--task-definitions", definition,
                     check=False)
        for pool in kinds.get("cognito-pool", []):
            self.cli("cognito-idp", "delete-user-pool", "--user-pool-id", pool, check=False)
            log(f"aws user pool deleted: {pool}")
        for secret in kinds.get("secret", []):
            self.cli("secretsmanager", "delete-secret", "--secret-id", secret,
                     "--force-delete-without-recovery", check=False)
        for zone in kinds.get("route53-zone", []):
            self._delete_zone(zone)
            log(f"aws hosted zone deleted: {zone}")
        for bucket in kinds.get("s3-bucket", []):
            log(f"aws bucket {'deleted' if self._delete_bucket(bucket) else 'kept (retention)'}: "
                f"{bucket}")
        for snap in kinds.get("rds-snapshot", []):
            self.cli("rds", "delete-db-snapshot", "--db-snapshot-identifier", snap, check=False)
        instances = kinds.get("rds-instance", [])
        for instance in instances:
            self.cli("rds", "delete-db-instance", "--db-instance-identifier", instance,
                     "--skip-final-snapshot", "--delete-automated-backups", check=False)
        for instance in instances:
            self.run(["aws", "rds", "wait", "db-instance-deleted", "--db-instance-identifier",
                      instance, "--profile", self.profile, "--region", self.region], check=False)
            log(f"aws database deleted: {instance}")
        for group in kinds.get("security-group", []):
            for _ in range(18):  # network interfaces are released a little later
                if self.cli("ec2", "delete-security-group", "--group-id", group,
                            check=False) is not None:
                    log(f"aws security group deleted: {group}")
                    break
                time.sleep(10)
        for role in kinds.get("iam-role", []):
            for policy in self.cli("iam", "list-role-policies", "--role-name", role)["PolicyNames"]:
                self.cli("iam", "delete-role-policy", "--role-name", role, "--policy-name", policy)
            for attached in self.cli("iam", "list-attached-role-policies", "--role-name", role)[
                    "AttachedPolicies"]:
                self.cli("iam", "detach-role-policy", "--role-name", role,
                         "--policy-arn", attached["PolicyArn"])
            self.cli("iam", "delete-role", "--role-name", role, check=False)
            log(f"aws role deleted: {role}")
        return self.inventory(run)

    def _delete_zone(self, zone):
        records = self.cli("route53", "list-resource-record-sets", "--hosted-zone-id", zone)[
            "ResourceRecordSets"]
        changes = [{"Action": "DELETE", "ResourceRecordSet": r} for r in records
                   if r["Type"] not in ("NS", "SOA")]
        if changes:
            self.cli("route53", "change-resource-record-sets", "--hosted-zone-id", zone,
                     "--change-batch", json.dumps({"Changes": changes}))
        self.cli("route53", "delete-hosted-zone", "--id", zone, check=False)

    def _delete_bucket(self, bucket):
        lock = self.cli("s3api", "get-object-lock-configuration", "--bucket", bucket, check=False)
        locked = bool(lock) and lock.get("ObjectLockConfiguration", {}).get(
            "ObjectLockEnabled") == "Enabled"
        listing = self.cli("s3api", "list-object-versions", "--bucket", bucket) or {}
        items = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                 for v in listing.get("Versions", []) + listing.get("DeleteMarkers", [])]
        for start in range(0, len(items), 500):
            self.cli("s3api", "delete-objects", "--bucket", bucket, "--delete",
                     json.dumps({"Objects": items[start:start + 500], "Quiet": True}),
                     *(["--bypass-governance-retention"] if locked else []), check=False)
        uploads = self.cli("s3api", "list-multipart-uploads", "--bucket", bucket) or {}
        for upload in uploads.get("Uploads", []):
            self.cli("s3api", "abort-multipart-upload", "--bucket", bucket, "--key",
                     upload["Key"], "--upload-id", upload["UploadId"], check=False)
        return self.cli("s3api", "delete-bucket", "--bucket", bucket, check=False) is not None


# ---------------------------------------------------------------- GCP


class Gcp:
    name = "gcp"
    SQL = "https://sqladmin.googleapis.com/v1"
    TENANTS = "https://identitytoolkit.googleapis.com/v2"

    def __init__(self, config, *, run=run_command, http=http_json, paths=None):
        self.project, self.region = config["project"], config["region"]
        self.scheduler_region = config.get("scheduler_region", self.region)
        self.run, self.http, self.paths = run, http, paths

    def g(self, *args, check=True):
        result = self.run(["gcloud", *args, "--project", self.project, "--format", "json",
                           "--quiet"], check=check)
        if result.returncode:
            return None
        return _json(result)

    def token(self):
        return self.run(["gcloud", "auth", "print-access-token"]).stdout.strip()

    def rest(self, method, url, body=None):
        return self.http(method, url, self.token(), self.project, body)

    def enabled(self):
        return {s["config"]["name"] for s in self.g("services", "list", "--enabled") or []}

    @staticmethod
    def instance_id(run):
        return f"sova-test-pg-{run}"

    # -- up

    def up(self, lease):
        run = lease["run"]
        self.g("services", "enable", "sqladmin.googleapis.com", "iam.googleapis.com",
               "cloudscheduler.googleapis.com")
        instance = self.instance_id(run)
        if self.g("sql", "instances", "describe", instance, check=False) is None:
            operation = self.g(
                "sql", "instances", "create", instance, "--async",
                "--database-version=POSTGRES_17", "--edition=enterprise", "--tier=db-f1-micro",
                f"--region={self.region}", "--storage-type=SSD", "--storage-size=10",
                "--no-storage-auto-increase", f"--authorized-networks={lease['ip']}/32",
                "--ssl-mode=ENCRYPTED_ONLY", "--server-ca-mode=GOOGLE_MANAGED_CAS_CA",
                "--database-flags=cloudsql.logical_decoding=on", "--no-backup",
                "--availability-type=zonal", "--no-deletion-protection", "--no-final-backup")
            self.wait(operation)
        # gcloud has no label flag for Cloud SQL; the Admin API does.
        self.wait(self.rest("PATCH", f"{self.SQL}/projects/{self.project}/instances/{instance}",
                            {"settings": {"userLabels": {"sova-test": run}}}))
        password = self.paths.read_secret(self.paths.new_secret("cloudsql-root").name)
        self.paths.new_secret("cloudsql-role")
        self.wait(self.rest("PUT", f"{self.SQL}/projects/{self.project}/instances/{instance}"
                                   "/users?name=postgres",
                            {"name": "postgres", "password": password}))
        described = self.g("sql", "instances", "describe", instance)
        ip = next(a["ipAddress"] for a in described.get("ipAddresses", [])
                  if a.get("type") == "PRIMARY")
        certs = self.g("sql", "ssl", "server-ca-certs", "list", f"--instance={instance}") or []
        ca = self.paths.ca / f"cloudsql-{run}.pem"
        write_private(ca, "".join(c["cert"].strip() + "\n" for c in certs))
        state = {"instance": instance, "ip": ip, "dns_name": described.get("dnsName", "").rstrip("."),
                 "ca": str(ca), "connection_name": described.get("connectionName")}
        state["backstop"] = self.backstop(run, instance, parse(lease["expires_at"]))
        return state

    def wait(self, operation):
        name = (operation or {}).get("name")
        if name:
            self.g("sql", "operations", "wait", name, "--timeout=1800")

    def backstop(self, run, instance, expires, *, update=False):
        """A Cloud Scheduler job that deletes the instance after the lease, as a
        service account allowed to delete that one instance only."""
        account = f"sova-test-expiry-{run}"
        email = f"{account}@{self.project}.iam.gserviceaccount.com"
        at = expires + BACKSTOP_GRACE
        schedule = f"{at.minute} {at.hour} {at.day} {at.month} *"
        if not update:
            if self.g("iam", "service-accounts", "describe", email, check=False) is None:
                self.g("iam", "service-accounts", "create", account,
                       "--display-name", f"sova test expiry {run}",
                       "--description", "deletes this run's test database after its lease")
            condition = (f'expression=resource.name == "projects/{self.project}/instances/'
                         f'{instance}",title=sova-test-{run}-instance-only')
            for _ in range(12):  # a new service account is visible to IAM a little later
                if self.g("projects", "add-iam-policy-binding", self.project,
                          f"--member=serviceAccount:{email}", "--role=roles/cloudsql.admin",
                          f"--condition={condition}", check=False) is not None:
                    break
                time.sleep(5)
            else:
                raise CloudEnvError("GCP expiry role binding not created")
        job = ["--location", self.scheduler_region, "--schedule", schedule,
               "--time-zone", "Etc/UTC",
               "--uri", f"{self.SQL}/projects/{self.project}/instances/{instance}",
               "--http-method", "DELETE", "--oauth-service-account-email", email,
               "--oauth-token-scope", "https://www.googleapis.com/auth/cloud-platform",
               "--attempt-deadline", "60s", "--max-retry-attempts", "5"]
        verb = "update" if update else "create"
        if not update and self.g("scheduler", "jobs", "describe", account, "--location",
                                 self.scheduler_region, check=False) is not None:
            verb = "update"
        for _ in range(24):  # a just-enabled API or new account is not visible at once
            if self.g("scheduler", "jobs", verb, "http", account, *job, check=False) is not None:
                return {"job": account, "service_account": email, "at": iso(at)}
            time.sleep(10)
        self.g("scheduler", "jobs", verb, "http", account, *job)  # the error, once more
        return {"job": account, "service_account": email, "at": iso(at)}

    def extend(self, lease, expires):
        state = lease["clouds"].get("gcp") or {}
        if state.get("instance"):
            state["backstop"] = self.backstop(lease["run"], state["instance"], expires,
                                              update=True)

    # -- inventory and sweep

    def inventory(self, run):
        apis = self.enabled()
        found = []
        if "sqladmin.googleapis.com" in apis:
            for item in self.g("sql", "instances", "list") or []:
                if scoped(item["name"], run):
                    found.append(("sql-instance", item["name"]))
        if "storage.googleapis.com" in apis:
            for item in self.g("storage", "buckets", "list") or []:
                if scoped(item["name"], run):
                    found.append(("bucket", item["name"]))
        if "secretmanager.googleapis.com" in apis:
            for item in self.g("secrets", "list") or []:
                if scoped(item["name"].rsplit("/", 1)[-1], run):
                    found.append(("secret", item["name"].rsplit("/", 1)[-1]))
        if "run.googleapis.com" in apis:
            for item in self.g("run", "services", "list") or []:
                name = item["metadata"]["name"]
                region = item["metadata"].get("labels", {}).get(
                    "cloud.googleapis.com/location", self.region)
                if scoped(name, run):
                    found.append(("run-service", f"{name}@{region}"))
        if "dns.googleapis.com" in apis:
            for item in self.g("dns", "managed-zones", "list") or []:
                if scoped(item["name"], run):
                    found.append(("dns-zone", item["name"]))
        if "cloudscheduler.googleapis.com" in apis:
            for item in self.g("scheduler", "jobs", "list", "--location",
                               self.scheduler_region) or []:
                if scoped(item["name"].rsplit("/", 1)[-1], run):
                    found.append(("scheduler-job", item["name"].rsplit("/", 1)[-1]))
        if "identitytoolkit.googleapis.com" in apis:
            listing = self.rest("GET", f"{self.TENANTS}/projects/{self.project}/tenants")
            for item in listing.get("tenants", []):
                if scoped(item.get("displayName", ""), run):
                    found.append(("idp-tenant", item["name"].rsplit("/", 1)[-1]))
        accounts = []
        if "iam.googleapis.com" in apis:
            for item in self.g("iam", "service-accounts", "list") or []:
                if scoped(item["email"].split("@", 1)[0], run):
                    accounts.append(item["email"])
                    found.append(("service-account", item["email"]))
        for role, member in self._bindings(run):
            found.append(("iam-binding", f"{role} {member}"))
        return found

    def _bindings(self, run):
        policy = self.g("projects", "get-iam-policy", self.project) or {}
        found = []
        for binding in policy.get("bindings", []):
            for member in binding.get("members", []):
                kind, _, rest = member.partition(":")
                if kind in ("deleted", "serviceAccount") and "serviceAccount:" in member:
                    email = member.split("serviceAccount:", 1)[1].split("?", 1)[0]
                    if scoped(email.split("@", 1)[0], run):
                        found.append((binding["role"], member))
        return found

    def sweep(self, run, log):
        found = self.inventory(run)
        kinds = {}
        for kind, ident in found:
            kinds.setdefault(kind, []).append(ident)
        for job in kinds.get("scheduler-job", []):
            self.g("scheduler", "jobs", "delete", job, "--location", self.scheduler_region,
                   check=False)
            log(f"gcp scheduler job deleted: {job}")
        for service in kinds.get("run-service", []):
            name, region = service.split("@", 1)
            self.g("run", "services", "delete", name, "--region", region, check=False)
            log(f"gcp cloud run service deleted: {name}")
        operations = []
        for instance in kinds.get("sql-instance", []):
            operations.append(self.g("sql", "instances", "delete", instance, "--async",
                                     check=False))
        for operation, instance in zip(operations, kinds.get("sql-instance", [])):
            if operation:
                self.wait(operation)
            log(f"gcp database deleted: {instance}")
        for secret in kinds.get("secret", []):
            self.g("secrets", "delete", secret, check=False)
        for bucket in kinds.get("bucket", []):
            self.run(["gcloud", "storage", "rm", "--recursive", "--all-versions", "--quiet",
                      f"gs://{bucket}", "--project", self.project], check=False)
            self.g("storage", "buckets", "delete", f"gs://{bucket}", check=False)
            log(f"gcp bucket deleted: {bucket}")
        for zone in kinds.get("dns-zone", []):
            for record in self.g("dns", "record-sets", "list", "--zone", zone) or []:
                if record["type"] not in ("NS", "SOA"):
                    self.g("dns", "record-sets", "delete", record["name"], "--type",
                           record["type"], "--zone", zone, check=False)
            self.g("dns", "managed-zones", "delete", zone, check=False)
            log(f"gcp dns zone deleted: {zone}")
        for tenant in kinds.get("idp-tenant", []):
            self.rest("DELETE", f"{self.TENANTS}/projects/{self.project}/tenants/{tenant}")
            log(f"gcp identity tenant deleted: {tenant}")
        for role, member in self._bindings(run):
            self.g("projects", "remove-iam-policy-binding", self.project, f"--member={member}",
                   f"--role={role}", "--all", check=False)
        for email in kinds.get("service-account", []):
            self.g("iam", "service-accounts", "delete", email, check=False)
            log(f"gcp service account deleted: {email}")
        return self.inventory(run)


# ---------------------------------------------------------------- the switch


PROVIDERS = {"aws": Aws, "gcp": Gcp}


class Switch:
    def __init__(self, paths, *, providers=None, clock=now, out=print, agent=None, ip=None):
        self.paths, self.clock, self.out = paths, clock, out
        self._providers = providers
        self.agent = agent or LaunchAgent(paths)
        self._ip = ip

    def providers(self, names):
        if self._providers is not None:
            return {n: self._providers[n] for n in names}
        config = load_json(self.paths.config)
        if not config:
            raise CloudEnvError("Run `cloud-env.sh configure` first")
        return {n: PROVIDERS[n](config[n], paths=self.paths) for n in names}

    def lease(self):
        return load_json(self.paths.lease)

    def ip(self):
        if self._ip:
            return self._ip
        with urllib.request.urlopen(IP_URL, timeout=15) as response:
            return response.read().decode().strip()

    def up(self, hours, clouds):
        if not 0 < hours <= MAX_HOURS:
            raise CloudEnvError(f"A lease is 1 to {MAX_HOURS} hours")
        current = self.lease()
        if current and current["state"] == "partial":
            # Same run, same end: create again what failed (creation is idempotent).
            lease = current
            clouds = [n for n, s in lease["clouds"].items() if not s or s.get("error")]
            self.out(f"resuming run {lease['run']}: {', '.join(clouds)}")
        elif current:
            raise CloudEnvError(f"Run {current['run']} is {current['state']} until "
                                f"{current['expires_at']}; use extend, or down first")
        else:
            self.paths.ensure()
            start = self.clock()
            lease = {"run": secrets.token_hex(3), "state": "starting", "created_at": iso(start),
                     "expires_at": iso(start + timedelta(hours=hours)), "ip": self.ip(),
                     "clouds": {name: {} for name in clouds}}
            save_json(self.paths.lease, lease)  # before any resource exists, so down finds it
        self.agent.install()
        providers = self.providers(clouds)
        failures = {}
        with ThreadPoolExecutor(len(providers)) as pool:
            futures = {name: pool.submit(p.up, lease) for name, p in providers.items()}
            for name, future in futures.items():
                try:
                    lease["clouds"][name] = future.result()
                except Exception as error:  # noqa: BLE001 - report each cloud on its own
                    failures[name] = str(error)
                    lease["clouds"][name] = {"error": str(error)}
        self._pgpass(lease)
        lease["state"] = "partial" if failures else "up"
        save_json(self.paths.lease, lease)
        for name, error in failures.items():
            self.out(f"{name}: FAILED - {error}")
        self.out(f"run {lease['run']} {lease['state']} until {lease['expires_at']}")
        return 1 if failures else 0

    def _pgpass(self, lease):
        lines = []
        aws, gcp = lease["clouds"].get("aws") or {}, lease["clouds"].get("gcp") or {}
        if aws.get("host"):
            lines.append(f"{aws['host']}:5432:*:{aws['master']}:"
                         f"{self.paths.read_secret('rds-master')}")
        if gcp.get("ip"):
            lines.append(f"{gcp.get('dns_name') or gcp['ip']}:5432:*:postgres:"
                         f"{self.paths.read_secret('cloudsql-root')}")
        write_private(self.paths.pgpass, "".join(line + "\n" for line in lines))

    def extend(self, hours):
        lease = self.lease()
        if not lease:
            raise CloudEnvError("Nothing is on")
        if not 0 < hours <= MAX_HOURS:
            raise CloudEnvError(f"Extend by 1 to {MAX_HOURS} hours")
        expires = max(self.clock(), parse(lease["expires_at"])) + timedelta(hours=hours)
        if expires - self.clock() > timedelta(hours=MAX_HOURS):
            raise CloudEnvError(f"A lease ends at most {MAX_HOURS} hours from now")
        for name, provider in self.providers(list(lease["clouds"])).items():
            provider.extend(lease, expires)
        lease["expires_at"] = iso(expires)
        save_json(self.paths.lease, lease)
        self.out(f"run {lease['run']} now ends {lease['expires_at']}")
        return 0

    def down(self, reason="requested"):
        lease = self.lease()
        if not lease:
            self.out("nothing is on")
            self.agent.remove()
            return 0
        lease["state"] = "stopping"
        save_json(self.paths.lease, lease)
        left = {}
        for name, provider in self.providers(list(lease["clouds"])).items():
            try:
                remaining = provider.sweep(lease["run"], self.out)
            except Exception as error:  # noqa: BLE001 - a failed listing is not "empty"
                remaining = [("error", str(error))]
            if remaining:
                left[name] = remaining
        if left:
            for name, items in left.items():
                for kind, ident in items:
                    self.out(f"{name}: still there - {kind} {ident}")
            self.out(f"run {lease['run']} stays open; down (or the expiry agent) retries")
            return 2
        lease.update(state="down", ended_at=iso(self.clock()), reason=reason)
        save_json(self.paths.history / f"{lease['run']}.json", lease)
        self.paths.lease.unlink()
        self.paths.pgpass.unlink(missing_ok=True)
        for name in ("rds-master", "rds-role", "cloudsql-root", "cloudsql-role"):
            self.paths.secret(name).unlink(missing_ok=True)
        self.out(f"run {lease['run']} is down; nothing of it is left in "
                 f"{', '.join(lease['clouds'])}")
        self.agent.remove()
        return 0

    def expire(self):
        lease = self.lease()
        if not lease:
            self.agent.remove()
            return 0
        if self.clock() < parse(lease["expires_at"]):
            return 0
        self.out(f"{iso(self.clock())} lease of run {lease['run']} ended; turning it off")
        return self.down(reason="expired")

    def status(self):
        lease = self.lease()
        if not lease:
            self.out("nothing is on")
            return 0
        left = parse(lease["expires_at"]) - self.clock()
        self.out(f"run {lease['run']}: {lease['state']}, ends {lease['expires_at']} "
                 f"({'%dh %02dm left' % divmod(int(left.total_seconds()) // 60, 60) if left.total_seconds() > 0 else 'ended'})")
        for name, provider in self.providers(list(lease["clouds"])).items():
            state = lease["clouds"].get(name) or {}
            if state.get("error"):
                self.out(f"{name}: up failed - {state['error']}")
            if state.get("backstop"):
                self.out(f"{name}: cloud-side deletion at {state['backstop']['at']}")
            items = provider.inventory(lease["run"])
            self.out(f"{name}: {len(items)} resource(s)")
            for kind, ident in items:
                self.out(f"  {kind} {ident}")
        return 0

    def env(self):
        lease = self.lease()
        if not lease or lease["state"] not in ("up", "partial"):
            raise CloudEnvError("Nothing is on")
        self.out(render_env(lease, self.paths, load_json(self.paths.config) or {}))
        return 0


def render_env(lease, paths, config):
    """Shell exports for the live tests. Secret values never appear: the
    database DSNs use a passfile and passwords are read with ``$(cat file)``
    when the output is evaluated."""
    run, out = lease["run"], []

    def export(name, value):
        out.append(f"export {name}={shlex.quote(str(value))}")

    def from_file(name, secret):
        out.append(f'export {name}="$(cat {shlex.quote(str(paths.secret(secret)))})"')

    aws, gcp = lease["clouds"].get("aws") or {}, lease["clouds"].get("gcp") or {}
    if aws.get("host"):
        export("SOVA_TEST_AWS_LIVE", "1")
        export("SOVA_TEST_AWS_PROFILE", config.get("aws", {}).get("profile", ""))
        export("SOVA_TEST_AWS_REGION", config.get("aws", {}).get("region", ""))
        export("SOVA_TEST_AWS_RUN", run)
        export("SOVA_TEST_RDS_ID", aws["rds_id"])
        export("SOVA_TEST_RDS_DSN", f"host={aws['host']} port=5432 user={aws['master']} "
               f"dbname=postgres sslmode=verify-full sslrootcert={aws['ca']} "
               f"passfile={paths.pgpass}")
        from_file("SOVA_TEST_RDS_ROLE_PASSWORD", "rds-role")
    if gcp.get("ip"):
        host = (f"host={gcp['dns_name']} hostaddr={gcp['ip']}" if gcp.get("dns_name")
                else f"host={gcp['ip']}")
        export("SOVA_TEST_GCP_LIVE", "1")
        export("SOVA_TEST_GCP_PROJECT", config.get("gcp", {}).get("project", ""))
        export("SOVA_TEST_GCP_REGION", config.get("gcp", {}).get("region", ""))
        export("SOVA_TEST_GCP_RUN", run)
        export("SOVA_TEST_CLOUDSQL_ID", gcp["instance"])
        export("SOVA_TEST_CLOUDSQL_DSN", f"{host} port=5432 user=postgres dbname=postgres "
               f"sslmode=verify-full sslrootcert={gcp['ca']} passfile={paths.pgpass}")
        from_file("SOVA_TEST_CLOUDSQL_ROLE_PASSWORD", "cloudsql-role")
        from_file("SOVA_TEST_CLOUDSQL_ROOT_PASSWORD", "cloudsql-root")
    return "\n".join(out)


class LaunchAgent:
    """The local half of the automatic end: launchd runs ``expire`` every ten
    minutes while a lease exists (and at the next wake if one was missed)."""

    def __init__(self, paths, *, run=run_command, home=None):
        self.paths, self.run = paths, run
        self.plist = Path(home or Path.home()) / "Library/LaunchAgents" / f"{AGENT}.plist"

    def _domain(self):
        return f"gui/{os.getuid()}"

    def install(self):
        if sys.platform != "darwin":
            return
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        with open(self.plist, "wb") as handle:
            plistlib.dump({
                "Label": AGENT,
                "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "expire"],
                "EnvironmentVariables": {
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    "SOVA_TEST_ENV_STATE": str(self.paths.root.parent)},
                "StartInterval": 600, "RunAtLoad": False,
                "StandardOutPath": str(self.paths.log), "StandardErrorPath": str(self.paths.log),
            }, handle)
        self.run(["launchctl", "bootout", f"{self._domain()}/{AGENT}"], check=False)
        self.run(["launchctl", "bootstrap", self._domain(), str(self.plist)])

    def remove(self):
        if sys.platform != "darwin" or not self.plist.exists():
            return
        self.plist.unlink()
        # Last step: when launchd itself runs this, bootout ends this process.
        self.run(["launchctl", "bootout", f"{self._domain()}/{AGENT}"], check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cloud-env.sh", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    configure = sub.add_parser("configure")
    configure.add_argument("--aws-profile", required=True)
    configure.add_argument("--aws-region", required=True)
    configure.add_argument("--gcp-project", required=True)
    configure.add_argument("--gcp-region", required=True)
    up = sub.add_parser("up")
    up.add_argument("--hours", type=float, default=8)
    up.add_argument("--clouds", default="aws,gcp")
    extend = sub.add_parser("extend")
    extend.add_argument("--hours", type=float, required=True)
    for name in ("down", "expire", "status", "env"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    root = Path(os.environ.get("SOVA_TEST_ENV_STATE", Path.home() / ".sova-test-env")) / "cloud"
    paths = Paths(root)
    if args.command == "configure":
        paths.ensure()
        save_json(paths.config, {"aws": {"profile": args.aws_profile, "region": args.aws_region},
                                 "gcp": {"project": args.gcp_project, "region": args.gcp_region}})
        print(f"saved {paths.config}")
        return 0
    switch = Switch(paths, out=lambda line: print(line, flush=True))
    try:
        if args.command == "up":
            clouds = [c for c in args.clouds.split(",") if c]
            if not clouds or set(clouds) - set(PROVIDERS):
                raise CloudEnvError(f"--clouds is a subset of {','.join(PROVIDERS)}")
            return switch.up(args.hours, clouds)
        if args.command == "extend":
            return switch.extend(args.hours)
        return getattr(switch, args.command)()
    except CloudEnvError as error:
        print(f"cloud-env: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
