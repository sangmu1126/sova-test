"""Actual offline copy, killed worker and remote settlement before readback recovery.

No Kubernetes identity is fabricated. NativeSettlement is a test proof producer.
"""

from dataclasses import replace
import time

import psycopg
from psycopg import sql
import pytest

from qualification_pool_relay import Relay
from qualification_runtime import runtime_service_journey
from qualification_settlement import NativeSettlement, remote_sessions
from qualification_worker import WorkerProcess
from qualification_orders import observe
from test_order_http_handoff import http_business as http_business  # noqa: F401
from test_worker_process_journey import execution, orders

from sova.domain.contracts import ContractError
from sova.ports.execution import RecoveryRequired
from sova.qualification.requests import OrderIntent, reconcile
from sova.service.worker import Execution


pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


def wait_for(check, timeout=10):
    until = time.monotonic()+timeout
    while time.monotonic()<until:
        result = check()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError('Expected measured settlement boundary not observed')


def prepare_copy_observers(business, mode):
    # Match trigger definitions on both sides; do not weaken stage_target's
    # real structure comparison. Only the tagged test copy on target may sleep.
    for side in ('src','tgt'):
        with business['connect'](business['names'][side]) as conn:
            conn.execute('CREATE TABLE qualification_copy_audit(applies int NOT NULL); '
                'INSERT INTO qualification_copy_audit VALUES(0); '
                'CREATE FUNCTION qualification_count_copy() RETURNS trigger LANGUAGE plpgsql AS $$ '
                'BEGIN UPDATE qualification_copy_audit SET applies=applies+1; RETURN NULL; END $$; '
                'CREATE TRIGGER qualification_copy_count AFTER TRUNCATE ON orders '
                'FOR EACH STATEMENT EXECUTE FUNCTION qualification_count_copy()')
            conn.execute(sql.SQL("CREATE FUNCTION qualification_delay_copy() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF {} AND current_database()={} AND current_setting('application_name') LIKE 'mq-worker-%' "
                "AND NEW.id=1 THEN PERFORM pg_sleep(30); END IF; RETURN NEW; END $$; "
                'CREATE TRIGGER qualification_copy_delay BEFORE INSERT ON orders '
                'FOR EACH ROW EXECUTE FUNCTION qualification_delay_copy()').format(
                    sql.Literal(mode=='uncommitted'), sql.Literal(business['names']['tgt'])))


class Counted:
    def __init__(self, inner, calls):
        self.inner, self.adapter_id, self.calls = inner, inner.adapter_id, calls

    def validate(self, command):
        self.inner.validate(command)

    def apply(self, command):
        self.calls.append({'method':'apply','action':command.action,'digest':command.fingerprint})
        return self.inner.apply(command)

    def readback(self, command, now):
        self.calls.append({'method':'readback','action':command.action,'digest':command.fingerprint})
        return self.inner.readback(command, now)


@pytest.mark.parametrize('mode', ['committed','uncommitted'])
def test_real_effect_is_not_replayed_and_recovery_requires_remote_settlement(
        postgres_database, http_business, tmp_path, mode):
    prepare_copy_observers(http_business, mode)
    with runtime_service_journey(postgres_database, http_business, tmp_path,
                                  case_name='settlement-'+mode) as h:
        admission = h.approve_and_admit(h.register_forward(), 'settlement-approval')
        unit = admission['unit_digest']
        calls, observations = [], {}
        def counting(factory):
            def build(*args):
                result = factory(*args)
                return Execution(Counted(result.adapter, calls), result.measure)
            return build
        successor = h.worker(forward_factory=counting)
        successor.owner_id = 'settlement-successor'
        old = None
        settlement = None
        with orders(h) as (journal, client):
            before = OrderIntent('before','settlement-client','before','10')
            denied = OrderIntent('during','settlement-client','during','20')
            after = OrderIntent('after','settlement-client','after','30')
            assert client.submit(before)==200
            try:
                with Relay(h.business['dsn']) as relay:
                    old = WorkerProcess(h, label='old-worker', pause_after='copy_content',
                        business_dsn=relay.dsn, remote_after_apply=(mode=='committed'))
                    settlement = NativeSettlement(h, old, relay)
                    successor.quiescence = settlement
                    sleeping = wait_for(lambda: [s for s in remote_sessions(h,old.owner)
                                                if s['state']=='active' and s['wait_event']=='PgSleep'])
                    assert len(sleeping)==1
                    old_state = execution(h)
                    copy = [e for e in old_state['effects'] if e['action']=='copy_content']
                    assert len(copy)==1 and copy[0]['status']=='in_flight'
                    assert client.submit(denied)==503
                    if mode=='committed':
                        assert old.ready.exists()
                        with h.business['connect'](h.business['names']['tgt']) as conn:
                            assert conn.execute('SELECT count(*) FROM orders').fetchone()==(51,)
                    old.kill_owned_process()
                    old_process = old.stop()
                    assert old_process['intentional_sigkill'] and old_process['process_wait_completed']
                    wait_for(lambda: execution(h)['lease_expired'])
                    # Old process death and real lease expiry still do not
                    # settle the database request retained by the relay.
                    assert remote_sessions(h,old.owner)
                    assert successor.drive(h.tenant,h.app,unit) is None
                    assert not settlement.issued and not calls
                    assert h.status(unit)['state']!='HANDOFF_COMPLETE'
                    assert h.route.get()==h.business['names']['src']
                    observations['blocked_after_process_death'] = execution(h)
                    observations['remote_before_fence'] = remote_sessions(h,old.owner)
                    # Only the uniquely tagged old test worker's business DB
                    # backends are fenced, never another process or shared DB.
                    with psycopg.connect(h.business['dsn'], autocommit=True) as conn:
                        killed = conn.execute('SELECT pid,pg_terminate_backend(pid) FROM pg_stat_activity '
                            'WHERE application_name=%s AND datname=ANY(%s)',
                            (old.owner,list(h.business['names'].values()))).fetchall()
                    assert killed and all(ok for _,ok in killed)
                    wait_for(lambda: not remote_sessions(h,old.owner))
                    observations['fenced_backend_pids'] = [pid for pid,_ in killed]
                assert relay.stopped.is_set() and not any(t.is_alive() for t in relay.threads)
                assert not remote_sessions(h,old.owner)
                if mode=='committed':
                    result = successor.drive(h.tenant,h.app,unit)
                    assert result.state.value=='HANDOFF_COMPLETE'
                    assert h.route.get()==h.business['names']['tgt']
                    assert client.submit(denied)==client.submit(after)==200
                else:
                    # Current product preserves unresolved readback as UNKNOWN;
                    # release of its recovery lease remains blocked.
                    with pytest.raises(RecoveryRequired):
                        successor.drive(h.tenant,h.app,unit)
                    assert h.route.get()==h.business['names']['src']
                    assert h.status(unit)['state']!='HANDOFF_COMPLETE'
                    assert client.submit(denied)==503
                assert len(settlement.issued)==1
                proof = next(iter(settlement.issued.values()))
                with pytest.raises(ContractError):
                    settlement.authenticate(replace(proof, artifact_digest='0'*64))
                copy_calls = [c for c in calls if c['action']=='copy_content']
                assert copy_calls==[{'method':'readback','action':'copy_content','digest':copy[0]['digest']}]
                events = old.events()
                assert sum(e['event']=='apply_started' and e['action']=='copy_content' for e in events)==1
                assert sum(e['event']=='apply_committed' and e['action']=='copy_content' for e in events)==(mode=='committed')
                with h.business['connect'](h.business['names']['tgt']) as conn:
                    count = conn.execute('SELECT applies FROM qualification_copy_audit').fetchone()[0]
                    target_rows = conn.execute('SELECT count(*) FROM orders').fetchone()[0]
                assert count==(1 if mode=='committed' else 0)
                assert target_rows==(53 if mode=='committed' else 0)
                final = execution(h)
                final_copy = [e for e in final['effects'] if e['action']=='copy_content']
                assert final_copy[0]['status']==('confirmed_applied' if mode=='committed' else 'unknown')
                assert final['epoch']>old_state['epoch']
                report = reconcile(journal.snapshot(), observe(h.business,h.route.get(),journal))
                assert report['data_integrity']=='SATISFIED'
                assert report['counts']['confirmed_requests']==(3 if mode=='committed' else 1)
                assert report['counts']['http_error_attempts']==(1 if mode=='committed' else 2)
                h.record('effect-settlement-'+mode, admission=admission, old_process=old_process,
                    old_state=old_state, final_state=final, settlement_observations=settlement.observations,
                    observations=observations, proof_digests=list(settlement.issued),
                    local_native_proof_producer=True, operational_proof_issued=False,
                    kubernetes_quiescence_qualified=False, successor_calls=calls, old_trace=events,
                    committed_copy_audit=count, target_rows=target_rows, measurement=report,
                    migration_completed=(mode=='committed'), effect_apply_replayed=False)
            finally:
                if old is not None:
                    if old.process.poll() is None:
                        old.kill_owned_process()
                    old.stop()
