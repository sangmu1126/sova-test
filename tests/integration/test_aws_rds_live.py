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

import os
from uuid import uuid4

from adapter_conformance import SEQUENCES, HandoffAdapterConformance, apply, readback
from postgres_business import PROBE_INSERT, TABLES, prepare
from postgres_world import PostgresWorld
import psycopg
from psycopg import sql
import pytest

from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes
from sova.domain.contracts import EffectStatus

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
    names = {"src": f"src_{suffix}", "tgt": f"tgt_{suffix}"}
    app, batch = "orders_app_" + suffix, "orders_batch_" + suffix
    servers = {names["src"]: rds, names["tgt"]: local}
    for dsn, database, secret in ((rds, names["src"], password), (local, names["tgt"], None)):
        with psycopg.connect(dsn, autocommit=True) as admin:
            for role in (app, batch):
                admin.execute(sql.SQL("CREATE ROLE {} LOGIN" + (" PASSWORD {}" if secret else ""))
                              .format(sql.Identifier(role),
                                      *([sql.Literal(secret)] if secret else [])))
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

    def connect(database):
        return psycopg.connect(servers[database], dbname=database)

    def connect_as(database, role):
        extra = {"password": password} if servers[database] is rds else {}
        return psycopg.connect(servers[database], dbname=database, user=role, **extra)

    prepare(names, app, batch, connect, connect_as, schema_on=(names["src"], names["tgt"]))
    return dict(names=names, app=app, batch=batch, connect=connect, connect_as=connect_as)


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
