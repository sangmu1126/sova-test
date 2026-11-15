"""Cloud SQL for PostgreSQL (the GCP source) to a local PostgreSQL (on premises).

The same reference adapters, conformance kit and control plane as between two
local databases and from RDS, with the source on a managed database of a
second provider: its master user is not a superuser (``cloudsqlsuperuser``),
it accepts only encrypted connections, and its certificate is verified
against the instance's CA under the instance's DNS name.

Opt-in and run-scoped like every ``gcp_live`` test: ``cloud-env.sh up`` creates
``sova-test-pg-<run>``; ``cloud-env.sh env`` names it and its passwords.
"""

from datetime import UTC, datetime, timedelta
import re
import time
from uuid import uuid4

from adapter_conformance import SEQUENCES, HandoffAdapterConformance, apply, readback
from gcp_fixtures import gcloud, primary_ip
from handoff_drive import run_handoff, without_transport_control
from postgres_business import PROBE_INSERT, TABLES
from postgres_world import PostgresWorld
import psycopg
import pytest

from sova.adapters.postgres_governance import PostgresSecurityHolds
from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes, artifact_digest
from sova.adapters.postgres_logical_replication import replication_name
from sova.application.source_fence_watch import SourceFenceWatch
from sova.domain.contracts import EffectStatus
from sova.domain.evidence import VerificationResult
from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.gcp_live, pytest.mark.postgres]


class TestCloudSqlToOnPremConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, cloudsql_business):
        return PostgresWorld(cloudsql_business)


def probes(b):
    return PostgresHandoffProbes(
        connect_as=b["connect_as"], admin_connect=b["connect"], source_db=b["names"]["src"],
        target_db=b["names"]["tgt"], app_role=b["app"], tables=TABLES,
        probe_insert=PROBE_INSERT, writer_roles=(b["app"], b["batch"]))


def test_a_full_handoff_from_cloud_sql_ends_with_one_writer_and_equal_data(cloudsql_business):
    b = cloudsql_business
    lingering = b["connect_as"](b["names"]["src"], b["app"])  # an app session on Cloud SQL
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


def test_transport_encryption_is_observed_not_assumed(cloudsql_business):
    p = probes(cloudsql_business)
    encrypted, observation = p.transport_encrypted("source")
    assert encrypted and observation["ssl"] == "on"  # Cloud SQL, ENCRYPTED_ONLY
    assert p.transport_encrypted("target")[0] is False  # the local reference server


def test_an_unencrypted_connection_is_refused_by_the_instance(cloudsql):
    """The instance itself refuses plaintext; the platform does not rely on
    every client choosing TLS."""
    plain = re.sub(r"sslmode=\S+", "sslmode=disable", cloudsql["dsn"])
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(plain, password=cloudsql["root_password"], connect_timeout=15).close()


def test_the_control_plane_hands_off_from_cloud_sql_to_on_premises(postgres_database, now,
                                                                   cloudsql_business):
    """The whole protocol, not only the adapter: approval, admission, authority
    record, ledger commands and signed measured evidence, with Cloud SQL as source."""
    b = cloudsql_business
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
    print(f"measured write-admission pause Cloud SQL->on-prem (offline-copy): "
          f"{pause * 1000:.0f} ms")


def _slots(b):
    with b["connect"](b["names"]["src"]) as conn:
        return [r[0] for r in conn.execute(
            "SELECT slot_name FROM pg_replication_slots WHERE slot_name LIKE 'sova_cdc_%'")]


def test_changes_stream_from_cloud_sql_and_only_the_tail_moves_inside_the_fence(
        postgres_database, now, cloudsql_cdc):
    """snapshot-cdc from a managed source: the local server subscribes to
    Cloud SQL over verified TLS, the data moves before the fence and only the
    changes still in flight are applied inside it; nothing is left behind on
    the managed instance afterwards."""
    b = cloudsql_cdc
    x = run_handoff(postgres_database, now, b, adjust=without_transport_control,
                    strategy="snapshot-cdc", cdc=True)
    assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                               x["record"].held_from)
    for table, _ in TABLES:
        assert x["probes"].content_equal(table)[0], table
    assert x["probes"].order_effects_once()[0]
    assert x["probes"].write_denied("source")[0]
    name = replication_name(b["names"]["src"], b["names"]["tgt"])
    assert name not in _slots(b)  # the slot on Cloud SQL was dropped
    with b["connect"](b["names"]["src"]) as conn:
        assert not conn.execute("SELECT 1 FROM pg_publication WHERE pubname = %s",
                                (name,)).fetchall()
    with b["connect"](b["names"]["tgt"]) as conn:
        assert not conn.execute("SELECT 1 FROM pg_subscription WHERE subname = %s",
                                (name,)).fetchall()
    observed = x["observed"]
    pause = (observed["activate_target"] - observed["pause_admission"]).total_seconds()
    print(f"measured write-admission pause Cloud SQL->on-prem (snapshot-cdc): "
          f"{pause * 1000:.0f} ms")


def _restore_target(gcp, cloudsql):
    """A new instance in the same network posture, to restore a backup into."""
    restored = f"sova-test-pgr-{gcp['run']}"
    if gcloud(gcp, "sql", "instances", "describe", restored, check=False) is None:
        networks = ",".join(n["value"] for n in cloudsql["described"]["settings"][
            "ipConfiguration"].get("authorizedNetworks", []))
        operation = gcloud(
            gcp, "sql", "instances", "create", restored, "--async",
            "--database-version=POSTGRES_17", "--edition=enterprise", "--tier=db-f1-micro",
            f"--region={gcp['region']}", "--storage-type=SSD", "--storage-size=10",
            "--no-storage-auto-increase", f"--authorized-networks={networks}",
            "--ssl-mode=ENCRYPTED_ONLY", "--server-ca-mode=GOOGLE_MANAGED_CAS_CA",
            "--no-backup", "--availability-type=zonal", "--no-deletion-protection",
            "--no-final-backup")
        gcloud(gcp, "sql", "operations", "wait", operation["name"], "--timeout=1800")
    return restored


def test_a_restored_cloud_sql_backup_is_caught_by_the_fence_watch(
        gcp, cloudsql, postgres_database, cloudsql_business, tmp_path):
    """AT-32 with a real restore: an on-demand backup taken while the
    application could still write is restored after the handoff into another
    instance. The restored database carries the old grants; a fresh probe
    there fails and the service is held."""
    b = cloudsql_business
    backup = gcloud(gcp, "sql", "backups", "create", f"--instance={cloudsql['instance']}",
                    f"--description=sova-test-{gcp['run']}-{uuid4().hex[:6]}", "--async")
    gcloud(gcp, "sql", "operations", "wait", backup["name"], "--timeout=1800")
    backup_id = max((item for item in gcloud(gcp, "sql", "backups", "list",
                                             f"--instance={cloudsql['instance']}")
                     if item.get("type") == "ON_DEMAND"),
                    key=lambda item: item["windowStartTime"])["id"]
    restored = _restore_target(gcp, cloudsql)
    try:
        x = run_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1), b,
                        adjust=without_transport_control)
        assert x["record"].state == HandoffState.HANDOFF_COMPLETE, (x["record"].reason,
                                                                   x["record"].held_from)
        holds = PostgresSecurityHolds(x["connect"], tenant_id=x["tenant"])
        watch = SourceFenceWatch(store=x["store"], evidence=x["coordinator"].evidence,
                                 holds=holds, clock=lambda: datetime.now(UTC))

        def observe(p):
            passed, observation = p.write_denied("source")
            x["prove"]("fence.source_writes_denied",
                       VerificationResult.PASS if passed else VerificationResult.FAIL,
                       artifact_digest=artifact_digest(observation))
            return watch.check(x["revision"], x["plan"], actor_id="fence-watch")

        assert observe(x["probes"])["status"] == "FENCED"
        operation = gcloud(gcp, "sql", "backups", "restore", backup_id,
                           f"--restore-instance={restored}",
                           f"--backup-instance={cloudsql['instance']}", "--async")
        gcloud(gcp, "sql", "operations", "wait", operation["name"], "--timeout=1800")
        described = gcloud(gcp, "sql", "instances", "describe", restored)
        assert described["name"] != cloudsql["instance"]
        assert primary_ip(described) != cloudsql["ip"]  # a different database server
        ca = tmp_path / "restored-ca.pem"
        ca.write_text("".join(c["cert"].strip() + "\n" for c in gcloud(
            gcp, "sql", "ssl", "server-ca-certs", "list", f"--instance={restored}")))
        host = described.get("dnsName") or primary_ip(described)  # with its final dot
        restored_dsn = (f"host={host} hostaddr={primary_ip(described)} port=5432 user=postgres "
                        f"dbname=postgres sslmode=verify-full sslrootcert={ca}")
        source = b["names"]["src"]

        def connect_as(database, role):
            if database == source:
                return psycopg.connect(restored_dsn, dbname=database, user=role,
                                       password=b["password"])
            return b["connect_as"](database, role)

        def connect(database):
            if database == source:  # the backup carries the master password too
                return psycopg.connect(restored_dsn, dbname=database,
                                       password=cloudsql["root_password"])
            return b["connect"](database)

        on_restored = PostgresHandoffProbes(
            connect_as=connect_as, admin_connect=connect, source_db=source,
            target_db=b["names"]["tgt"], app_role=b["app"], tables=TABLES,
            probe_insert=PROBE_INSERT, writer_roles=(b["app"], b["batch"]))
        caught = observe(on_restored)
        assert caught["status"] == "SOURCE_WRITER_REACTIVATED" and caught["newly_opened"], caught
        assert x["store"].active_hold(x["app"])
    finally:
        gcloud(gcp, "sql", "instances", "delete", restored, "--async", check=False)
        for _ in range(3):  # the backup goes too: nothing of the run outlives it
            if gcloud(gcp, "sql", "backups", "delete", backup_id,
                      f"--instance={cloudsql['instance']}", check=False) is not None:
                break
            time.sleep(10)
