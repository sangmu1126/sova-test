"""Explicitly isolated real PostgreSQL fixtures shared by repository suites."""

import os
from uuid import uuid4
from datetime import timedelta
import psycopg
from psycopg import sql
import pytest
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.service import schema
from sova.domain.contracts import ResourceScope
from sova.domain.evidence import RevisionSet, VerificationObligation, Gate
from sova.domain.plans import (
    LifecyclePlan,
    PlanStep,
    PlanPurpose,
    PlanApproval,
    Inspection,
)


@pytest.fixture(scope="session")
def postgres_database():
    dsn = os.environ.get("SOVA_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("SOVA_TEST_POSTGRES_DSN is required for real PostgreSQL tests")
    role = "sova_runtime_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        assert admin.execute(
            "SELECT current_setting('cluster_name'),current_database()"
        ).fetchone() == ("sova-persistence-test", "sova_test")
        schema.apply(admin)  # the service's own migration runner and order
        admin.autocommit = True
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT"
            ).format(sql.Identifier(role))
        )
        schema.grant_runtime(admin, role)
    return dsn, role


@pytest.fixture
def postgres_bundle(postgres_database, now):
    dsn, role = postgres_database
    tenant = "tenant-" + uuid4().hex

    def connect():
        return psycopg.connect(dsn, user=role)

    repo = PostgresLifecycleRepository(connect, tenant_id=tenant, clock=lambda: now)
    scope = ResourceScope(tenant, "app", "environment", "owned-resource")
    revisions = RevisionSet("a" * 64, "b" * 64, "c" * 64, "d" * 64)
    obligation = VerificationObligation("owned", scope, Gate.PREFLIGHT, "v1")
    step = PlanStep(
        "isolate", scope, "test-ingress", "set_ingress", '{"blocked":true}', "r1"
    )
    plan = LifecyclePlan(
        "plan",
        tenant,
        "app",
        "author",
        PlanPurpose.CHANGE,
        revisions,
        now + timedelta(minutes=10),
        (step,),
        (obligation,),
    )
    approval = PlanApproval(
        plan.fingerprint, "approver", plan.purpose, plan.valid_until
    )
    inspection = Inspection(
        scope, revisions, now, plan.valid_until, '{"state":"known"}', "e" * 64
    )
    return repo, plan, approval, inspection, obligation


@pytest.fixture(scope="session")
def second_postgres_database(postgres_database):
    dsn = os.environ.get("SOVA_TEST_SECOND_POSTGRES_DSN")
    if not dsn:
        pytest.skip(
            "SOVA_TEST_SECOND_POSTGRES_DSN is required for independent-cluster tests"
        )
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute(
            "SELECT current_setting('cluster_name'),current_database()"
        ).fetchone() == ("sova-persistence-target-test", "sova_target_test")
        second_id = conn.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
    with psycopg.connect(postgres_database[0]) as conn:
        first_id = conn.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
    assert first_id != second_id
    return dsn
