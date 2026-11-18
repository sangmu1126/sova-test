"""Real relayed TLS faults and role-generation drain, not operational acceptance."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg_pool import PoolClosed
import pytest

from qualification_pool import ControlPool
from qualification_pool_relay import Child, Relay
from sova.service import schema

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


def record(name, **value):
    if directory := os.environ.get('SOVA_TEST_QUALIFICATION_ARTIFACTS'):
        with (Path(directory)/(name+'.json')).open('x') as file:
            json.dump({'version': 1, 'case': name, 'qualification_issued': False,
                       'authorizes_execution': False, **value}, file, indent=2)
            file.write('\n')


@pytest.mark.parametrize('stall', [False, True])
def test_real_tls_checkout_reveals_queue_timeout_is_not_a_full_network_deadline(postgres_database, stall):
    dsn, role = postgres_database
    name = 'pool-checkout-stalled' if stall else 'pool-checkout-healthy'
    facts = {'server_response_stalled': stall, 'business_effect_executed': False, 'complete': False}
    with Relay(dsn) as relay:
        child = Child(relay.dsn, role, 'checkout')
        try:
            ready = child.receive(5)
            assert ready['event']=='ready'
            if stall:
                relay.pause.set()
            child.send('go')
            assert child.receive(2)=={'event':'started'}
            started = time.monotonic()
            if stall:
                assert relay.blocked.wait(timeout=2)
                assert child.receive(1.3) is None and child.process.poll() is None
                facts['blocked_observation_ms'] = round((time.monotonic()-started)*1000,3)
                with psycopg.connect(dsn) as admin:
                    state = admin.execute('SELECT state FROM pg_stat_activity WHERE pid=%s', (ready['pid'],)).fetchone()
                assert state is not None
                facts['actual_backend_state'] = state[0]
                facts['queue_timeout_seconds'] = 1
                facts['queue_timeout_did_not_bound_checkout'] = True
            relay.pause.clear()
            outcome = child.receive(4)
            assert outcome['event'] in ('acquired','rejected'), outcome
            if not stall:
                assert outcome['event']=='acquired'
            facts['outcome_after_healing'] = outcome
            child.send('go')
            assert child.receive(4)=={'event':'closed'}
            assert child.process.wait(timeout=2)==0
            facts['complete'] = True
        finally:
            relay.pause.clear()
            child.close()
            facts.update(messages=child.messages, child_exit=child.process.returncode,
                         guard_forced_stop=child.forced_stop, held_tls_bytes=relay.held_bytes)
            record(name, **facts)
    assert not facts['guard_forced_stop']


def test_stalled_real_reset_does_not_lend_the_unclean_backend(postgres_database):
    dsn, role = postgres_database
    facts = {'complete': False, 'business_effect_executed': False}
    with Relay(dsn) as relay:
        child = Child(relay.dsn, role, 'reset')
        try:
            assert child.receive(5)['event']=='ready'
            child.send('go')
            assert child.receive(2)['event']=='borrowed'
            relay.pause.set()
            child.send('go')
            assert child.receive(2)=={'event':'returned'}
            assert relay.blocked.wait(timeout=2)
            result = child.receive(3)
            assert result['event']=='rejected' and result['error_class']=='PoolTimeout', result
            facts['next_acquisition'] = result
            facts['unclean_backend_not_lent'] = True
            relay.pause.clear()
            child.send('go')
            assert child.receive(4)=={'event':'closed'}
            assert child.process.wait(timeout=2)==0
            facts['complete'] = True
        finally:
            relay.pause.clear()
            child.close()
            facts.update(messages=child.messages, child_exit=child.process.returncode,
                         guard_forced_stop=child.forced_stop, held_tls_bytes=relay.held_bytes)
            record('pool-reset-stalled', **facts)
    assert not facts['guard_forced_stop']


def roles(dsn, *, grant_new=True):
    old, new = 'old_'+uuid4().hex, 'new_'+uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        for role in (old, new):
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT').format(sql.Identifier(role)))
        schema.grant_runtime(admin, old)
        if grant_new:
            schema.grant_runtime(admin, new)
    return old, new


def wait_queued(pool):
    until = time.monotonic()+2
    while pool.pool.get_stats().get('requests_waiting',0)!=1:
        if time.monotonic()>=until:
            raise AssertionError('Queued borrower was not observed')
        time.sleep(.01)


@pytest.mark.parametrize('new_ready', [True, False])
def test_generation_drain_rejects_queued_work_and_never_falls_back_to_old_role(postgres_database, new_ready):
    dsn, _ = postgres_database
    old, new = roles(dsn, grant_new=new_ready)
    facts = {'complete': False, 'new_role_has_runtime_grants': new_ready,
             'password_or_certificate_rotation_tested': False, 'business_effect_executed': False}
    try:
        with ControlPool(dsn, old, max_size=1) as previous:
            with previous.connect() as active:
                pid = active.info.backend_pid
                assert active.execute('SELECT current_user').fetchone()==(old,)
                def borrow():
                    with previous.connect():
                        raise AssertionError('Old generation should not lend again')
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(borrow)
                    wait_queued(previous)
                    with psycopg.connect(dsn, autocommit=True) as admin:
                        admin.execute(sql.SQL('ALTER ROLE {} NOLOGIN').format(sql.Identifier(old)))
                    previous.pool.close(timeout=1)
                    with pytest.raises(PoolClosed):
                        pending.result(timeout=2)
                assert not active.closed
                assert active.execute('SELECT 1').fetchone()==(1,)
                facts['old_active_session_survived_close_and_nologin'] = True
                facts['queued_borrower_rejected'] = True
            assert active.closed
            with psycopg.connect(dsn) as admin:
                assert admin.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=%s', (pid,)).fetchone()==(0,)
            facts['old_session_closed_before_new_open'] = True
        with ControlPool(dsn, new, max_size=1) as candidate:
            if new_ready:
                with candidate.connect() as conn:
                    assert conn.info.backend_pid!=pid
                    assert conn.execute('SELECT current_user,(SELECT count(*) FROM sova_control.plans)').fetchone()==(new,0)
                facts['new_generation_accepted'] = True
            else:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with candidate.connect() as conn:
                        conn.execute('SELECT count(*) FROM sova_control.plans')
                facts['new_generation_accepted'] = False
        with pytest.raises(PoolClosed):
            with previous.connect():
                raise AssertionError('No old-generation fallback permitted')
        facts['no_old_generation_fallback'] = True
        facts['complete'] = True
    finally:
        record('pool-generation-valid' if new_ready else 'pool-generation-denied', **facts)


def test_urgent_old_role_fence_terminates_active_session_without_replaying_work(postgres_database):
    dsn, _ = postgres_database
    old, new = roles(dsn)
    facts = {'complete': False, 'business_effect_executed': False,
             'inflight_commit_ambiguity_qualified': False, 'password_or_certificate_rotation_tested': False}
    try:
        with ControlPool(dsn, old, max_size=1) as previous:
            with previous.connect() as active:
                pid = active.info.backend_pid
                assert active.execute('SELECT current_user').fetchone()==(old,)
                with psycopg.connect(dsn, autocommit=True) as admin:
                    admin.execute(sql.SQL('ALTER ROLE {} NOLOGIN').format(sql.Identifier(old)))
                    assert admin.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()==(True,)
                with pytest.raises(psycopg.Error):
                    active.execute('SELECT 1')
            previous.pool.close(timeout=1)
        with ControlPool(dsn, new, max_size=1) as candidate:
            with candidate.connect() as active:
                assert active.info.backend_pid!=pid and active.execute('SELECT current_user').fetchone()==(new,)
        facts.update(complete=True, old_active_session_fenced=True, new_identity_verified=True,
                     effect_retry_added=False)
    finally:
        record('pool-generation-urgent-fence', **facts)
