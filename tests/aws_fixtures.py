"""Opt-in fixtures for tests against a real AWS account.

Nothing here runs unless ``SOVA_TEST_AWS_LIVE=1`` names the intent and
``SOVA_TEST_AWS_PROFILE``, ``SOVA_TEST_AWS_REGION`` and ``SOVA_TEST_AWS_RUN``
(a short run id) are set. Every resource these tests create carries
``sova-test-<run>`` in its name and a ``sova-test`` tag, so a run-scoped
cleanup removes exactly what was created and nothing else. Credentials come
from the named local profile and are never printed.
"""

import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4

from botocore.config import Config
from kind_support import delete_namespace, kind_namespace, kubectl
from postgres_business import prepare
import psycopg
from psycopg import sql
import pytest

from sova.adapters.aws_cognito_directory import CognitoUserPoolDirectory
from sova.adapters.keycloak_identity import KeycloakAdmin

TRUST_WAIT_SECONDS = 90


@pytest.fixture(scope="session")
def aws():
    if os.environ.get("SOVA_TEST_AWS_LIVE") != "1":
        pytest.skip("SOVA_TEST_AWS_LIVE=1 is required for tests against a real AWS account")
    boto3 = pytest.importorskip("boto3")
    run = os.environ.get("SOVA_TEST_AWS_RUN", "")
    if not re.fullmatch(r"[a-z0-9]{4,8}", run):
        raise pytest.UsageError("SOVA_TEST_AWS_RUN must be a short lowercase run id")
    session = boto3.Session(profile_name=os.environ["SOVA_TEST_AWS_PROFILE"],
                            region_name=os.environ["SOVA_TEST_AWS_REGION"])
    caller = session.client("sts").get_caller_identity()["Arn"]
    return {"session": session, "run": run, "region": session.region_name, "caller": caller,
            "tags": [{"Key": "sova-test", "Value": run}]}


@pytest.fixture(scope="module")
def aws_writer_roles(aws):
    """Two IAM roles the caller may assume: the application and a batch job.
    They hold no permissions of their own; bucket policies decide."""
    iam = aws["session"].client("iam")
    trust = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": aws["caller"]}, "Action": "sts:AssumeRole"}]})
    roles = {}
    for writer in ("app", "batch"):
        name = f"sova-test-{writer}-{aws['run']}"
        try:
            arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust,
                                  MaxSessionDuration=3600, Tags=aws["tags"],
                                  Description="sova test writer, deleted after the run")["Role"]["Arn"]
        except iam.exceptions.EntityAlreadyExistsException:
            arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        roles[writer] = arn
    sts = aws["session"].client("sts")
    clients = {}
    deadline = time.monotonic() + TRUST_WAIT_SECONDS
    for writer, arn in roles.items():
        while True:  # IAM is eventually consistent: wait until the role can be assumed
            try:
                credentials = sts.assume_role(RoleArn=arn, RoleSessionName="sova-test",
                                              DurationSeconds=3600)["Credentials"]
                break
            except sts.exceptions.ClientError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(3)
        clients[writer] = credentials
    time.sleep(10)  # bucket policies resolve new principals a little later still
    yield {"arn": roles, "credentials": clients}
    for writer in roles:
        iam.delete_role(RoleName=f"sova-test-{writer}-{aws['run']}")


@pytest.fixture
def cloud_business(aws, postgres_database):
    rds, password = os.environ.get("SOVA_TEST_RDS_DSN"), os.environ.get(
        "SOVA_TEST_RDS_ROLE_PASSWORD")
    if not rds or not password:
        pytest.skip("SOVA_TEST_RDS_DSN and SOVA_TEST_RDS_ROLE_PASSWORD are required")
    if f"sova-test-pg-{aws['run']}." not in rds:
        raise pytest.UsageError("Only this run's RDS instance may be used")
    local = postgres_database[0]
    suffix = uuid4().hex[:10]
    names = {k: f"{k}_{suffix}" for k in ("src", "tgt", "scratch", "other")}
    app, batch = "orders_app_" + suffix, "orders_batch_" + suffix
    # The source on RDS; the target, a restore scratch and another tenant on premises.
    servers = {names["src"]: rds, **{names[k]: local for k in ("tgt", "scratch", "other")}}
    for dsn, databases, secret in ((rds, ("src",), password),
                                   (local, ("tgt", "scratch", "other"), None)):
        with psycopg.connect(dsn, autocommit=True) as admin:
            for role in (app, batch):
                admin.execute(sql.SQL("CREATE ROLE {} LOGIN" + (" PASSWORD {}" if secret else ""))
                              .format(sql.Identifier(role),
                                      *([sql.Literal(secret)] if secret else [])))
            for key in databases:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(names[key])))
                if key in ("src", "tgt"):
                    admin.execute(sql.SQL("COMMENT ON DATABASE {} IS 'approved-zone-a'").format(
                        sql.Identifier(names[key])))
            if "other" in databases:
                admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(names["other"])))

    def connect(database):
        return psycopg.connect(servers[database], dbname=database)

    def connect_as(database, role):
        extra = {"password": password} if servers[database] is rds else {}
        return psycopg.connect(servers[database], dbname=database, user=role, **extra)

    prepare(names, app, batch, connect, connect_as,
            schema_on=(names["src"], names["tgt"], names["scratch"]))
    return dict(names=names, app=app, batch=batch, connect=connect, connect_as=connect_as,
                servers=servers, password=password)


def create_bucket_in(aws, client, bucket, **extra):
    client.create_bucket(Bucket=bucket, CreateBucketConfiguration={
        "LocationConstraint": aws["region"]}, **extra)
    client.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": aws["tags"]})


@pytest.fixture(scope="module")
def cloud_to_onprem(aws, aws_writer_roles, s3_stores):
    """Stores for S3World: source on AWS S3, target on the local gateway."""
    config = Config(retries={"max_attempts": 3}, s3={"addressing_style": "virtual"})
    admin = aws["session"].client("s3", config=config)

    def role_client(writer):
        c = aws_writer_roles["credentials"][writer]
        return aws["session"].client(
            "s3", aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"],
            aws_session_token=c["SessionToken"], config=config)

    writers = {"app": role_client("app"), "other": role_client("batch")}
    return {
        "root": lambda side: admin if side == "source" else s3_stores["root"]("target"),
        "as": lambda side, writer: (writers[writer] if side == "source"
                                    else s3_stores["as"]("target", writer)),
        "principals": {
            "source": {"app": aws_writer_roles["arn"]["app"],
                       "other": aws_writer_roles["arn"]["batch"]},
            "target": s3_stores["principals"]["target"]},
        "bucket_name": lambda side, suffix: (
            f"sova-test-src-{aws['run']}-{suffix}" if side == "source"
            else f"orders-tgt-{suffix}"),
        "create_bucket": lambda client, bucket: create_bucket_in(aws, client, bucket),
    }




IMAGE = "public.ecr.aws/docker/library/busybox:1.37.0"


def ecs_tags(aws):
    return [{"key": t["Key"], "value": t["Value"]} for t in aws["tags"]]


@pytest.fixture
def ecs_app(aws):
    session, run = aws["session"], aws["run"]
    ecs, ec2 = session.client("ecs"), session.client("ec2")
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets(Filters=[
        {"Name": "vpc-id", "Values": [vpc["VpcId"]]},
        {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]]
    me = urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10).read().decode()
    group = ec2.create_security_group(
        GroupName=f"sova-test-ecs-{run}-{uuid4().hex[:6]}", VpcId=vpc["VpcId"],
        Description="sova test source app, this machine only",
        TagSpecifications=[{"ResourceType": "security-group", "Tags": aws["tags"]}])["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=group, IpProtocol="tcp", FromPort=80,
                                         ToPort=80, CidrIp=me.strip() + "/32")
    cluster, service = f"sova-test-ecs-{run}", f"sova-test-orders-{run}"
    token = "sova-" + run + "-" + secrets.token_hex(8)
    ecs.create_cluster(clusterName=cluster, tags=ecs_tags(aws))
    definition = ecs.register_task_definition(
        family=f"sova-test-app-{run}", networkMode="awsvpc", cpu="256", memory="512",
        requiresCompatibilities=["FARGATE"], tags=ecs_tags(aws),
        runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        containerDefinitions=[{
            "name": "app", "image": IMAGE, "essential": True,
            "portMappings": [{"containerPort": 80, "protocol": "tcp"}],
            "command": ["sh", "-c", f"mkdir -p /www && echo {token} > /www/index.html"
                        " && exec httpd -f -p 80 -h /www"]}],
    )["taskDefinition"]["taskDefinitionArn"]
    try:
        ecs.create_service(
            cluster=cluster, serviceName=service, taskDefinition=definition, desiredCount=1,
            launchType="FARGATE", tags=ecs_tags(aws), propagateTags="SERVICE",
            networkConfiguration={"awsvpcConfiguration": {
                "subnets": subnets, "securityGroups": [group], "assignPublicIp": "ENABLED"}})
        ecs.get_waiter("services_stable").wait(cluster=cluster, services=[service],
                                               WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        yield {"ecs": ecs, "ec2": ec2, "cluster": cluster, "service": service, "token": token}
    finally:
        ecs.update_service(cluster=cluster, service=service, desiredCount=0)
        ecs.delete_service(cluster=cluster, service=service, force=True)
        ecs.get_waiter("services_inactive").wait(cluster=cluster, services=[service],
                                                 WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        ecs.deregister_task_definition(taskDefinition=definition)
        ecs.delete_task_definitions(taskDefinitions=[definition])
        ecs.delete_cluster(cluster=cluster)
        for _ in range(18):  # the task's network interface is released a little later
            try:
                ec2.delete_security_group(GroupId=group)
                break
            except ec2.exceptions.ClientError:
                time.sleep(10)


def answers(address, token):
    """True only if the source application itself answers at ``address``."""
    try:
        with urllib.request.urlopen(f"http://{address}/", timeout=5) as response:
            return token in response.read(4096).decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False




def _password():
    return "Sv1!" + secrets.token_urlsafe(18)


@pytest.fixture
def cognito(aws):
    idp = aws["session"].client("cognito-idp")
    pool = idp.create_user_pool(
        PoolName=f"sova-test-pool-{aws['run']}-{uuid4().hex[:6]}", UserPoolTier="LITE",
        MfaConfiguration="OFF", DeletionProtection="INACTIVE",
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        UserPoolTags={t["Key"]: t["Value"] for t in aws["tags"]},
        Policies={"PasswordPolicy": {"MinimumLength": 12, "RequireUppercase": True,
                                     "RequireLowercase": True, "RequireNumbers": True,
                                     "RequireSymbols": True}})["UserPool"]["Id"]
    try:
        client = idp.create_user_pool_client(
            UserPoolId=pool, ClientName="orders-web", GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        )["UserPoolClient"]["ClientId"]
        for group in ("finance", "support"):
            idp.create_group(UserPoolId=pool, GroupName=group)
        passwords = {}
        for name, groups, enabled in (("alice", ("finance",), True),
                                      ("bob", ("finance", "support"), True),
                                      ("carol", (), False)):
            idp.admin_create_user(
                UserPoolId=pool, Username=name, MessageAction="SUPPRESS",
                UserAttributes=[{"Name": "email", "Value": f"{name}@example.test"},
                                {"Name": "email_verified", "Value": "true"},
                                {"Name": "given_name", "Value": name.title()},
                                {"Name": "family_name", "Value": "Tester"}])
            passwords[name] = _password()
            idp.admin_set_user_password(UserPoolId=pool, Username=name,
                                        Password=passwords[name], Permanent=True)
            for group in groups:
                idp.admin_add_user_to_group(UserPoolId=pool, Username=name, GroupName=group)
            if not enabled:
                idp.admin_disable_user(UserPoolId=pool, Username=name)
        yield {"idp": idp, "pool": pool, "client": client, "passwords": passwords,
               "directory": CognitoUserPoolDirectory(idp, pool)}
    finally:
        idp.delete_user_pool(UserPoolId=pool)


def cognito_login(c, username, password):
    """Cognito has no password grant at its token endpoint; its API signs in."""
    try:
        result = c["idp"].initiate_auth(
            ClientId=c["client"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password})
    except c["idp"].exceptions.ClientError as error:
        return None, error.response["Error"]["Code"]
    return result["AuthenticationResult"], None


@pytest.fixture
def keycloak_target(keycloak_servers, realm_name):
    issuer = keycloak_servers["target"].realm(realm_name)
    user, password = keycloak_servers["admin"]
    admin = KeycloakAdmin(keycloak_servers["target"].url, realm_name, username=user,
                          password=password)
    return {"server": keycloak_servers["target"], "realm": realm_name, "issuer": issuer,
            "admin": admin}


def reset_password(onprem, username, password):
    found = onprem["server"].find(onprem["realm"], username)
    onprem["server"].call("PUT", f"/{onprem['realm']}/users/{found['id']}/reset-password",
                          {"type": "password", "value": password, "temporary": False})
    onprem["server"].call("PUT", f"/{onprem['realm']}/users/{found['id']}",
                          {**found, "requiredActions": []})
    return found




SECRET_TARGETS = ("orders-db", "orders-api")


@pytest.fixture(scope="module")
def kind_secrets():
    rules = [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": list(SECRET_TARGETS),
              "verbs": ["get", "patch"]}]
    objects = [{"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name}} for name in SECRET_TARGETS]
    objects.append({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                    "metadata": {"name": "unrelated"},
                    "data": {"token": base64.b64encode(b"not-for-the-handoff").decode()}})
    context, namespace, controls = kind_namespace(rules, objects, prefix="secrets")
    yield {"context": context, "namespace": namespace, "controls": controls}
    delete_namespace(context, namespace)


@pytest.fixture
def cloud_secrets(aws):
    sm = aws["session"].client("secretsmanager")
    base = f"sova-test/{aws['run']}/{uuid4().hex[:6]}"
    values = {"db": json.dumps({"username": "orders",
                                "password": secrets.token_urlsafe(24)}).encode(),
              "api": secrets.token_bytes(32)}
    names = {"db": f"{base}/orders-db", "api": f"{base}/orders-api-key"}
    created = [sm.create_secret(Name=names["db"], SecretString=values["db"].decode(),
                                Tags=aws["tags"])["ARN"],
               sm.create_secret(Name=names["api"], SecretBinary=values["api"],
                                Tags=aws["tags"])["ARN"]]
    yield {"sm": sm, "names": names, "values": values}
    for arn in created:
        sm.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)


def stored_secret(onprem, name, key):
    encoded = kubectl(onprem["context"], "-n", onprem["namespace"], "get", "secret", name,
                      "-o", f"jsonpath={{.data.{key}}}").strip()
    return base64.b64decode(encoded) if encoded else None


def assert_no_value_in(text, values):
    for value in values:
        forms = {value, base64.b64encode(value)}
        if value.startswith(b"{"):
            forms.add(json.loads(value)["password"].encode())
        assert not any(form in text.encode() for form in forms), "a secret value leaked"


@pytest.fixture
def hosted_zone(aws):
    if not shutil.which("dig"):
        pytest.skip("dig is required to ask the authoritative servers")
    r53 = aws["session"].client("route53")
    name = f"sova-test-{aws['run']}-{uuid4().hex[:6]}.com"
    created = r53.create_hosted_zone(Name=name, CallerReference=uuid4().hex,
                                     HostedZoneConfig={"Comment": "sova test, run-scoped"})
    zone_id = created["HostedZone"]["Id"].rsplit("/", 1)[-1]
    r53.change_tags_for_resource(ResourceType="hostedzone", ResourceId=zone_id,
                                 AddTags=aws["tags"])
    try:
        yield {"r53": r53, "id": zone_id, "name": name,
               "servers": created["DelegationSet"]["NameServers"]}
    finally:
        records = r53.list_resource_record_sets(HostedZoneId=zone_id)["ResourceRecordSets"]
        changes = [{"Action": "DELETE", "ResourceRecordSet": r} for r in records
                   if r["Type"] not in ("NS", "SOA")]
        if changes:
            r53.change_resource_record_sets(HostedZoneId=zone_id,
                                            ChangeBatch={"Changes": changes})
        r53.delete_hosted_zone(Id=zone_id)


def authoritative(zone, record):
    """What the zone's own name servers answer, asked over DNS."""
    answers = set()
    for server in zone["servers"]:
        out = subprocess.run(["dig", "+short", "+time=3", "+tries=2", f"@{server}", record, "A"],
                             capture_output=True, text=True, timeout=30).stdout.split()
        answers.add(tuple(sorted(out)))
    return answers
