"""AWS RDS for PostgreSQL (the cloud source) to a local PostgreSQL (on premises).

The specification's first reference path (§29.1), measured: the same
reference adapter and conformance kit as between two local databases, with
the source on a managed database the platform does not administer as a
superuser. Opt-in and run-scoped like every ``aws_live`` test:
``SOVA_TEST_RDS_DSN`` is the master connection of this run's instance
(``sova-test-pg-<run>``, TLS verified against the RDS CA bundle) and
``SOVA_TEST_RDS_ROLE_PASSWORD`` the password given to the writer roles the
tests create there.
"""

from datetime import UTC, datetime, timedelta
import os
import re
import time
from uuid import uuid4

from adapter_conformance import SEQUENCES, HandoffAdapterConformance, apply, readback
from handoff_drive import run_handoff, without_transport_control
from postgres_business import PROBE_INSERT, TABLES, prepare
from postgres_world import PostgresWorld
import psycopg
from psycopg import sql
import pytest

from sova.adapters.aws_rds_security import RdsSecurityCollector, fixed_clients
from sova.adapters.postgres_governance import PostgresSecurityHolds
from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes, artifact_digest
from sova.application.source_fence_watch import SourceFenceWatch
from sova.domain.contracts import EffectStatus, ResourceScope
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import HandoffState
from sova.domain.aws_security import RdsReadRegistration

pytestmark = [pytest.mark.aws_live, pytest.mark.postgres]


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


class TestRdsToOnPremConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, cloud_business):
        return PostgresWorld(cloud_business)


def probes(b):
    return PostgresHandoffProbes(
        connect_as=b["connect_as"], admin_connect=b["connect"], source_db=b["names"]["src"],
        target_db=b["names"]["tgt"], app_role=b["app"], tables=TABLES,
        probe_insert=PROBE_INSERT, writer_roles=(b["app"], b["batch"]))


def test_a_full_handoff_from_rds_ends_with_one_writer_and_equal_data(cloud_business):
    b = cloud_business
    lingering = b["connect_as"](b["names"]["src"], b["app"])  # an app session on RDS
    lingering.execute("SELECT 1")
    world = PostgresWorld(b)
    for action in SEQUENCES["offline-copy"]:
        apply(world.adapter(), action)
        assert readback(world.adapter(), action) == EffectStatus.APPLIED, action
    # The fence ended the session on the managed database too (no superuser there).
    with pytest.raises(psycopg.OperationalError):
        lingering.execute("INSERT INTO orders VALUES (900, 'late', 'late', 1)")
    lingering.close()
    p = probes(b)
    assert p.single_writer()[0]
    for table, _ in TABLES:
        assert p.content_equal(table)[0], table
    assert p.order_effects_once()[0]
    with b["connect_as"](b["names"]["tgt"], b["app"]) as conn:  # business continues
        conn.execute("INSERT INTO orders VALUES (51, 'customer-1', 'key-51', 510)")
        assert conn.execute("SELECT nextval('order_number_seq')").fetchone()[0] == 11


def test_transport_encryption_is_observed_not_assumed(cloud_business):
    p = probes(cloud_business)
    encrypted, observation = p.transport_encrypted("source")
    assert encrypted and observation["ssl"] == "on"  # RDS
    assert p.transport_encrypted("target")[0] is False  # the local reference server


def _instance(aws):
    return os.environ.get("SOVA_TEST_RDS_ID") or f"sova-test-pg-{aws['run']}"


def test_the_control_plane_hands_off_from_rds_to_on_premises(postgres_database, now,
                                                             cloud_business):
    """The whole protocol, not only the adapter: approval, admission, authority
    record, ledger commands and signed measured evidence, with RDS as source."""
    b = cloud_business
    x = run_handoff(postgres_database, now, b, adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                               x["record"].held_from)
    assert x["record"].owner_environment == "onprem-target"
    assert x["route"].get() == b["names"]["tgt"]
    for table, _ in TABLES:
        assert x["probes"].content_equal(table)[0], table
    assert x["probes"].order_effects_once()[0]
    assert x["probes"].write_denied("source")[0]
    observed = x["observed"]
    pause = (observed["activate_target"] - observed["pause_admission"]).total_seconds()
    print(f"measured write-admission pause RDS->on-prem: {pause * 1000:.0f} ms")
    with b["connect_as"](b["names"]["tgt"], b["app"]) as conn:
        conn.execute("INSERT INTO orders VALUES (51, 'customer-1', 'key-51', 510)")


def _wait(rds, waiter, **kwargs):
    rds.get_waiter(waiter).wait(**kwargs, WaiterConfig={"Delay": 20, "MaxAttempts": 90})


def test_a_snapshot_restored_rds_source_is_caught_by_the_fence_watch(
        aws, postgres_database, now, cloud_business):
    """AT-32 with a real restore: a snapshot taken while the application could
    still write is restored after the handoff. The restored database carries
    the old grants; a fresh probe there fails and the service is held."""
    b, rds = cloud_business, aws["session"].client("rds")
    restored_id = os.environ.get("SOVA_TEST_RDS_RESTORED_ID") or f"sova-test-pgr-{aws['run']}"
    if not restored_id.startswith("sova-test-") or aws["run"] not in restored_id:
        raise pytest.UsageError("Only this run's restore target may be created")
    snapshot = f"sova-test-snap-{aws['run']}-{uuid4().hex[:6]}"
    rds.create_db_snapshot(DBSnapshotIdentifier=snapshot, DBInstanceIdentifier=_instance(aws),
                           Tags=aws["tags"])
    _wait(rds, "db_snapshot_available", DBSnapshotIdentifier=snapshot)

    # Approvals are bound to their time; the snapshot took minutes, so take it anew.
    x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), b,
                    adjust=without_transport_control)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                               x["record"].held_from)
    holds = PostgresSecurityHolds(x["connect"], tenant_id=x["tenant"])
    watch = SourceFenceWatch(store=x["store"], evidence=x["coordinator"].evidence,
                             holds=holds, clock=lambda: datetime.now(UTC))

    def observe(probes):
        passed, observation = probes.write_denied("source")
        x["prove"]("fence.source_writes_denied",
                   VerificationResult.PASS if passed else VerificationResult.FAIL,
                   artifact_digest=artifact_digest(observation))
        return watch.check(x["revision"], x["plan"], actor_id="fence-watch")

    assert observe(x["probes"])["status"] == "FENCED"
    original = rds.describe_db_instances(DBInstanceIdentifier=_instance(aws))["DBInstances"][0]
    rds.restore_db_instance_from_db_snapshot(
        DBInstanceIdentifier=restored_id, DBSnapshotIdentifier=snapshot,
        DBInstanceClass="db.t4g.micro", PubliclyAccessible=True, MultiAZ=False,
        VpcSecurityGroupIds=[g["VpcSecurityGroupId"] for g in original["VpcSecurityGroups"]],
        Tags=aws["tags"])
    _wait(rds, "db_instance_available", DBInstanceIdentifier=restored_id)
    restored = rds.describe_db_instances(DBInstanceIdentifier=restored_id)["DBInstances"][0]
    assert restored["DbiResourceId"] != original["DbiResourceId"]  # a different database

    restored_dsn = re.sub(r"host=\S+", "host=" + restored["Endpoint"]["Address"],
                          b["servers"][b["names"]["src"]])
    servers = {**b["servers"], b["names"]["src"]: restored_dsn}

    def connect_as(database, role):
        extra = {"password": b["password"]} if servers[database] == restored_dsn else {}
        return psycopg.connect(servers[database], dbname=database, user=role, **extra)

    def connect(database):
        return psycopg.connect(servers[database], dbname=database)

    on_restored = PostgresHandoffProbes(
        connect_as=connect_as, admin_connect=connect, source_db=b["names"]["src"],
        target_db=b["names"]["tgt"], app_role=b["app"], tables=TABLES,
        probe_insert=PROBE_INSERT, writer_roles=(b["app"], b["batch"]))
    caught = observe(on_restored)
    assert caught["status"] == "SOURCE_WRITER_REACTIVATED" and caught["newly_opened"], caught
    assert x["store"].active_hold(x["app"])


def test_the_rds_security_reader_reports_the_real_configuration(aws):
    """The existing read-only RDS collector, until now tested on recorded SDK
    responses, against the real instance. This test instance is deliberately
    public, unencrypted and without deletion protection; the reader must say so."""
    session, rds = aws["session"], aws["session"].client("rds")
    db = rds.describe_db_instances(DBInstanceIdentifier=_instance(aws))["DBInstances"][0]
    tenant, app = "tenant-orders", "orders"
    rds.add_tags_to_resource(ResourceName=db["DBInstanceArn"], Tags=[
        {"Key": "sova:managed-by", "Value": "sova"}, {"Key": "sova:tenant-id", "Value": tenant},
        {"Key": "sova:application-id", "Value": app}])
    issued = datetime.now(UTC) - timedelta(seconds=5)
    registration = RdsReadRegistration(
        ResourceScope(tenant, app, "aws-source", db["DBInstanceArn"]),
        session.client("sts").get_caller_identity()["Account"], aws["region"],
        db["DbiResourceId"], 1, 1, issued, issued + timedelta(hours=1))
    for _ in range(30):  # tags and status settle shortly after a change
        snapshot = RdsSecurityCollector(
            registration, clients=lambda reg: fixed_clients(session, reg),
            authorize=lambda *_: None, clock=lambda: datetime.now(UTC),
            monotonic=time.monotonic).collect(actor="aws-live-test")
        if "unknown" not in snapshot.checks.values():
            break
        time.sleep(10)
    # AWS omits EnabledCloudwatchLogsExports when no log is exported, and the
    # reader treats a missing field as unknown: never a pass, but a real gap
    # that cannot show as a failure either (recorded for the security package).
    assert "EnabledCloudwatchLogsExports" not in db
    assert snapshot.checks == {
        "storage-encryption": "pass" if db["StorageEncrypted"] else "fail",
        "backup-retention": "pass", "private-endpoint": "fail",
        "deletion-protection": "fail", "postgres-log-export": "unknown",
        "multi-az-configuration": "fail"}, snapshot.checks
