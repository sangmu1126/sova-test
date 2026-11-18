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
from sova.adapters.postgres_operations import PostgresOperationRepository
from sova.adapters.postgres_execution import PostgresExecutionOwnership
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.adapters.ed25519_evidence import Ed25519EvidenceVerifier
from sova.domain.contracts import ContractError
from sova.domain.operations import OperationRequest
from sova.domain.evidence import EvidenceRecord, VerificationResult
from sova.ports.execution import ExecutionBusy
from sova.ports.operations import LeaseLost
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
    tenant = 'tenant-'+uuid4().hex
    other_plan = replace(plan, tenant_id=tenant,
        steps=tuple(replace(s, scope=replace(s.scope, tenant_id=tenant)) for s in plan.steps),
        obligations=tuple(replace(o, scope=replace(o.scope, tenant_id=tenant)) for o in plan.obligations))
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


def test_failed_reset_discards_the_backend_instead_of_lending_dirty_state(postgres_database, monkeypatch):
    normal = ControlPool.reset
    attempted = []
    def reset(conn):
        if not attempted:
            attempted.append(True)
            raise RuntimeError('expected reset failure')
        normal(conn)
    monkeypatch.setattr(ControlPool, 'reset', staticmethod(reset))
    with ControlPool(*postgres_database, max_size=1) as pool:
        with pool.connect() as c:
            pid = c.info.backend_pid
            c.execute("SET sova.tenant_id='must-not-reappear'")
        with pool.connect() as c:
            assert c.info.backend_pid != pid
            assert c.execute("SELECT nullif(current_setting('sova.tenant_id',true),'')").fetchone() == (None,)
        assert pool.pool.get_stats()['returns_bad'] == 1


def test_dead_backend_is_replaced_before_any_caller_query(postgres_database):
    dsn, _ = postgres_database
    with ControlPool(*postgres_database, max_size=1) as pool:
        with pool.connect() as c:
            pid = c.info.backend_pid
        with psycopg.connect(dsn, autocommit=True) as admin:
            assert admin.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone() == (True,)
        with pool.connect() as c:
            assert c.info.backend_pid != pid
            assert c.execute('SELECT 1').fetchone() == (1,)


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


def test_connect_privilege_revocation_is_not_bypassed_by_existing_session(postgres_database):
    dsn, role = postgres_database
    database = sql.Identifier('sova_test')
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(database, sql.Identifier(role)))
        admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(database))
    try:
        with ControlPool(dsn, role, max_size=1) as pool:
            with pool.connect() as c:
                assert c.execute('SELECT 1').fetchone() == (1,)
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(database, sql.Identifier(role)))
            with pytest.raises(PoolTimeout):
                with pool.connect():
                    pytest.fail('existing session ignored CONNECT revocation')
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO PUBLIC').format(database))


def test_revoked_approval_cannot_be_admitted_through_a_warm_pool(pool, postgres_bundle):
    fresh, plan, approval, *_ = postgres_bundle
    fresh.save_plan(plan)
    fresh.save_approval('app', 'plan', 'pool-approval', approval)
    repo = PostgresLifecycleRepository(pool.connect, tenant_id=plan.tenant_id)
    assert not repo.get_approval('app', 'pool-approval').revoked
    fresh.revoke_approval('app', 'pool-approval', expected_revision=1, actor_id='revoker')
    assert repo.get_approval('app', 'pool-approval').revoked
    with pytest.raises(ContractError, match='authorize'):
        PostgresOperationRepository(repo).submit(OperationRequest(plan.tenant_id, 'app', 'pooled-operation',
            'plan', plan.fingerprint, 'pool-approval', 1, 'operator', 'pool-request'))


def test_ownership_epoch_and_expired_owner_checks_survive_reuse(pool, postgres_bundle):
    fresh, plan, approval, *_ = postgres_bundle
    repo = PostgresLifecycleRepository(pool.connect, tenant_id=plan.tenant_id)
    repo.save_plan(plan)
    repo.save_approval('app', 'plan', 'epoch-approval', approval)
    PostgresOperationRepository(repo).submit(OperationRequest(plan.tenant_id, 'app', 'epoch-operation',
        'plan', plan.fingerprint, 'epoch-approval', 1, 'operator', 'epoch-request'))
    owners = PostgresExecutionOwnership(repo)
    old = owners.acquire('app', 'epoch-operation', 'owner-old')
    with pytest.raises(ExecutionBusy):
        owners.acquire('app', 'epoch-operation', 'owner-new')
    with fresh.transaction() as c:
        c.execute("UPDATE sova_control.app_execution SET valid_until=clock_timestamp()-interval '1 second' "
                  'WHERE application_id=%s', ('app',))
    new = owners.acquire('app', 'epoch-operation', 'owner-new')
    assert new.epoch == old.epoch+1 and new.token != old.token
    with pytest.raises(LeaseLost):
        owners.renew(old)
    with pytest.raises(LeaseLost):
        owners.release(old)
    owners.release(new)


def test_current_signed_evidence_trust_revocation_is_not_cached(postgres_database, http_business, tmp_path):
    from datetime import timedelta
    with service_journey(postgres_database, http_business, tmp_path) as h:
        compiled = h.register_forward()
        plan = h.approve(compiled, 'trust-approval')
        o = plan.obligations[0]
        at = h.clock()-timedelta(milliseconds=1)
        record = EvidenceRecord('pool-evidence', o.obligation_id, o.scope, plan.revisions,
            h.settings.evidence_verifier, at, at+timedelta(minutes=1), VerificationResult.PASS,
            'observation', 'e'*64)
        envelope = h.signer.sign(record, issued_at=at)
        with ControlPool(*postgres_database) as pool:
            registry = PostgresVerifierRegistry(pool.connect, tenant_id=h.tenant, application_id=h.app)
            verifier = Ed25519EvidenceVerifier(registry.resolve)
            verifier.verify(envelope, expected_record=record, now=h.clock())
            prior = registry.latest(h.signer.issuer_id, h.signer.key_id)
            h.publish_trust(replace(prior.trust, revoked=True), version=prior.revision+1)
            with pytest.raises(ContractError, match='revoked'):
                verifier.verify(envelope, expected_record=record, now=h.clock())


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
