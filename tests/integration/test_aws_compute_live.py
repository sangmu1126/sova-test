"""Leaving AWS for good: data on RDS and compute on ECS, measured on premises.

The unit's source data is a run-scoped RDS for PostgreSQL instance and its
source application a run-scoped ECS Fargate service (one small task with a
public address open to this machine only). The application answers with a
token unique to the run, so an address AWS later hands to someone else is
never mistaken for it. After the control-plane handoff to on-premises
PostgreSQL, the adapter stops the ECS service and reads ECS back, the source
database stops accepting connections, and each exit drill is measured: the
old application is gone, a new session does business on the target, and a
restore works there. Provider exit then follows from measurements, as the
rule says, and platform exit, not measured here, is not claimed.
"""

from datetime import UTC, datetime, timedelta
import secrets
import time
import urllib.error
import urllib.request
from uuid import uuid4

from handoff_drive import run_handoff, without_transport_control
import psycopg
from psycopg import sql
import pytest

from sova.adapters.aws_ecs_compute import EcsServiceCompute
from sova.adapters.postgres_handoff_probes import artifact_digest
from sova.domain.contracts import EffectCommand, EffectStatus, ResourceScope
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.aws_live, pytest.mark.postgres]
IMAGE = "public.ecr.aws/docker/library/busybox:1.37.0"


def _tags(aws):
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
    ecs.create_cluster(clusterName=cluster, tags=_tags(aws))
    definition = ecs.register_task_definition(
        family=f"sova-test-app-{run}", networkMode="awsvpc", cpu="256", memory="512",
        requiresCompatibilities=["FARGATE"], tags=_tags(aws),
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
            launchType="FARGATE", tags=_tags(aws), propagateTags="SERVICE",
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


def command(adapter):
    return EffectCommand(
        ResourceScope("tenant-orders", "orders", "aws-source", "unit-compute"), "op-compute",
        "stop", adapter.adapter_id, "stop_source_compute", "op-compute:stop", "b" * 64,
        "revision-1", 1, datetime.now(UTC) + timedelta(minutes=10), "{}")


def test_leaving_aws_data_and_compute_is_measured_drill_by_drill(
        ecs_app, postgres_database, cloud_business):
    b = cloud_business
    names = b["names"]
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), b,
                    adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (
        x["record"].reason, x["record"].held_from, x["observed"].get("uncertain"),
        x["observed"].get("failed"))
    compute = EcsServiceCompute("ecs-source-compute", ecs=ecs_app["ecs"], ec2=ecs_app["ec2"],
                                cluster=ecs_app["cluster"], service=ecs_app["service"])
    old = compute.endpoints()
    assert len(old) == 1 and answers(old[0], ecs_app["token"])  # the source app serves

    def source_compute_blocked():
        stopped, ecs_state = compute.observe()
        answered = answers(old[0], ecs_app["token"])
        serves, target = x["probes"].cold_start_without_source()
        return stopped and not answered and serves, {
            "probe": "source_compute_blocked", "ecs": ecs_state,
            "old_endpoint": "answered" if answered else "unreachable", "target": target}

    passed, before = source_compute_blocked()  # the probe is not blind
    assert not passed and before["old_endpoint"] == "answered"
    assert before["ecs"]["running"] == 1

    cmd = command(compute)
    compute.validate(cmd)
    compute.apply(cmd)
    observation = compute.readback(cmd, datetime.now(UTC))
    assert observation.status == EffectStatus.APPLIED, observation.evidence
    with psycopg.connect(b["servers"][names["src"]], autocommit=True) as admin:  # RDS
        admin.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
            sql.Identifier(names["src"])))
    measured = {
        "exit.compute.source_compute_blocked": source_compute_blocked,
        "exit.removed.source-db.blocked_cold_start": x["probes"].cold_start_without_source,
        "exit.restart.cold_start_fresh_credentials": x["probes"].cold_start_without_source,
        "exit.operations.batch_backup_restore_audit_deploy":
            lambda: x["probes"].restore_drill(names["scratch"]),
    }
    observations = {}
    for o in x["readiness"].obligations(x["revision"], x["plan"]):
        if o.obligation_id in measured:
            passed, observations[o.obligation_id] = measured[o.obligation_id]()
            x["prove"](o.obligation_id, VerificationResult.PASS if passed else
                       VerificationResult.FAIL, obligation=o,
                       artifact_digest=artifact_digest(observations[o.obligation_id]))
    assert observations["exit.compute.source_compute_blocked"]["old_endpoint"] == "unreachable"
    levels = {k: v["status"] for k, v in
              x["readiness"].assess(x["revision"], x["plan"])["levels"].items()}
    assert levels["COMPUTE_INDEPENDENT"] == "PASS", levels  # undecidable locally
    assert levels["DATA_IDENTITY_INDEPENDENT"] == "PASS"
    assert levels["RESTART_VERIFIED"] == "PASS" and levels["OPERATIONS_VERIFIED"] == "PASS"
    # Provider exit is the four levels above, all measured against AWS sources.
    assert levels["PROVIDER_EXIT_VERIFIED"] == "PASS"
    assert levels["PLATFORM_EXIT_VERIFIED"] == "UNKNOWN"  # not measured: not claimed
