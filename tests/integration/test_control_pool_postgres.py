"""Native TLS tests of reuse boundaries, not deployed/Pod continuity qualification."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from threading import Barrier
from time import perf_counter_ns
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg_pool import PoolClosed, PoolTimeout
import pytest

from qualification_pool import ControlPool
from qualification_service import service_journey
from qualification_timing import read_ledger
from test_order_http_handoff import http_business as http_business  # noqa: F401
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.ports.plans import RecordNotFound
from sova.service import schema
from sova.service.worker import HandoffWorker

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


@pytest.fixture
def pool(postgres_database):
    with ControlPool(*postgres_database, max_size=2) as value:
        yield value


def test_session_settings_temp_objects_prepared_statements_and_locks_are_reset(postgres_database):
    with ControlPool(*postgres_database, max_size=1) as pool:
        with pool.connect() as c:
            pid = c.info.backend_pid
            c.execute("SET sova.tenant_id='dirty-tenant'")
            c.execute("SET search_path=pg_catalog")
            c.execute('CREATE TEMP TABLE dirty_temp(x int)')
            c.execute('PREPARE dirty_plan AS SELECT 1')
            c.execute('SELECT pg_advisory_lock(12456789)')
        with pool.connect() as c:
            assert c.info.backend_pid == pid and c.info.transaction_status == TransactionStatus.IDLE
            assert c.execute("SELECT nullif(current_setting('sova.tenant_id',true),''),"
                "to_regclass('pg_temp.dirty_temp'),(SELECT count(*) FROM pg_prepared_statements),"
                "(SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory')").fetchone() == (None, None, 0, 0)
            assert c.execute('SHOW search_path').fetchone() == ('"$user", public',)
        assert pool.pool.get_stats()['connections_num'] == 1


@pytest.mark.parametrize('failure', ['sql-error', 'application-error', 'cancelled-query'])
def test_exception_or_query_cancellation_is_rolled_back_before_reuse(postgres_database, failure):
    with ControlPool(*postgres_database, max_size=1) as pool:
        with pytest.raises((psycopg.Error, RuntimeError)):
            with pool.connect() as c:
                pid = c.info.backend_pid
                c.execute('CREATE TEMP TABLE failed_temp(x int)')
                if failure == 'sql-error':
                    c.execute('SELECT 1/0')
                elif failure == 'cancelled-query':
                    # Actual server cancellation, via its own statement timeout.
                    c.execute("SET LOCAL statement_timeout='20ms'")
                    c.execute('SELECT pg_sleep(1)')
                else:
                    raise RuntimeError('expected application abort')
        with pool.connect() as c:
            assert c.info.backend_pid == pid and c.info.transaction_status == TransactionStatus.IDLE
            assert c.execute("SELECT to_regclass('pg_temp.failed_temp')").fetchone() == (None,)
            assert c.execute('SELECT 1').fetchone() == (1,)


def test_transaction_attributes_are_reset_and_readonly_is_not_leaked(postgres_database):
    with ControlPool(*postgres_database, max_size=1) as pool:
        with pool.connect() as c:
            pid = c.info.backend_pid
            c.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            c.read_only = True
            c.deferrable = True
            c.execute('SELECT 1')
        with pool.connect() as c:
            assert c.info.backend_pid == pid
            assert c.isolation_level is c.read_only is c.deferrable is None
            assert c.execute("SHOW transaction_read_only").fetchone() == ('off',)


def test_rls_and_concurrent_tenants_remain_isolated(pool, postgres_bundle):
    original, plan, *_ = postgres_bundle
    a = PostgresLifecycleRepository(pool.connect, tenant_id=plan.tenant_id)
    other_plan = replace(plan, tenant_id='tenant-'+uuid4().hex)
    b = PostgresLifecycleRepository(pool.connect, tenant_id=other_plan.tenant_id)
    a.save_plan(plan)
    b.save_plan(other_plan)
    barrier = Barrier(2)
    def access(repo, expected):
        for _ in range(5):
            with repo.transaction() as c:
                barrier.wait(timeout=5)
                rows = c.execute('SELECT tenant_id FROM sova_control.plans').fetchall()
                assert rows and {r[0] for r in rows} == {expected}
        return True
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(access, a, plan.tenant_id), executor.submit(access, b, other_plan.tenant_id)]
        assert all(f.result(timeout=15) for f in futures)
    with pool.connect() as c:
        assert c.execute('SELECT count(*) FROM sova_control.plans').fetchone() == (0,)
    denied = PostgresLifecycleRepository(pool.connect, tenant_id='another-tenant')
    with pytest.raises(RecordNotFound):
        denied.get_plan(plan.application_id, plan.plan_id)


def test_pool_exhaustion_has_finite_wait_and_closed_pool_has_no_fallback(postgres_database):
    with ControlPool(*postgres_database, max_size=1) as pool:
        with pool.connect():
            started = perf_counter_ns()
            with pytest.raises(PoolTimeout):
                with pool.connect():
                    pytest.fail('pool exceeded max_size')
            assert (perf_counter_ns()-started)/1e9 < 2
    with pytest.raises(PoolClosed):
        with pool.connect():
            pytest.fail('closed pool fell back to a fresh connection')


@pytest.mark.parametrize('revocation', ['login', 'bypassrls'])
def test_live_role_revocation_is_checked_even_for_existing_backend(postgres_database, revocation):
    dsn, _ = postgres_database
    role = 'pool_role_'+uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT').format(sql.Identifier(role)))
        schema.grant_runtime(admin, role)
    with ControlPool(dsn, role, max_size=1) as pool:
        with pool.connect() as c:
            assert c.execute('SELECT 1').fetchone() == (1,)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL('ALTER ROLE {} '+('NOLOGIN' if revocation=='login' else 'BYPASSRLS')).format(sql.Identifier(role)))
        with pytest.raises(PoolTimeout):
            with pool.connect():
                pytest.fail('old session bypassed current role check')
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('ALTER ROLE {} LOGIN NOBYPASSRLS').format(sql.Identifier(role)))


@pytest.mark.parametrize('mode', ['fresh-before', 'pooled', 'fresh-after'])
def test_actual_handoff_preserves_data_and_current_checks(postgres_database, http_business, tmp_path, mode):
    with service_journey(postgres_database, http_business, tmp_path) as h:
        admission = h.approve_and_admit(h.register_forward(), 'pool-approval')
        normal = h.worker()
        context = ControlPool(*postgres_database) if mode=='pooled' else nullcontext()
        with context as pool:
            worker = HandoffWorker(pool.connect if pool else h.connect, settings=h.settings,
                catalog=normal.catalog, signer=h.signer, clock=h.clock, owner_id='pool-worker')
            started = perf_counter_ns()
            result = worker.drive(h.tenant, h.app, admission['unit_digest'])
            wall = (perf_counter_ns()-started)/1_000_000
            stats = pool.pool.get_stats() if pool else None
        assert result.state.value == 'HANDOFF_COMPLETE'
        ledger = read_ledger(h.connect, h.tenant, h.app, admission)
        assert len(ledger['effects'])==21 and len(ledger['authority'])==10 and len(ledger['measurement_receipts'])==16
        assert all(r['result']=='pass' for r in ledger['measurement_receipts'])
        assert h.route.get()==h.business['names']['tgt']
        with h.business['connect'](h.route.get()) as c:
            assert c.execute('SELECT (SELECT count(*) FROM orders),(SELECT count(*) FROM work_items),'
                             '(SELECT count(*) FROM idempotency_results)').fetchone()==(50,50,50)
        if stats:
            assert stats['connections_num'] <= 4 and stats.get('requests_errors',0)==0
        h.record('control-pool-'+mode, mode=mode, worker_wall_ms=round(wall,3), pool_stats=stats,
                 ledger=ledger, historical_rows=50, client_load=False, operational_qualification=False)
