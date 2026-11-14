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

from aws_fixtures import answers
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
