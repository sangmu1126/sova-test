"""Actual offline copy, killed worker and remote settlement before readback recovery.

No Kubernetes identity is fabricated. NativeSettlement is a test proof producer.
"""

from dataclasses import replace
import time
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from qualification_pool_relay import Relay
from qualification_runtime import runtime_service_journey
from qualification_settlement import NativeSettlement, remote_sessions
from qualification_worker import WorkerProcess
from qualification_orders import observe
from test_order_http_handoff import http_business as http_business  # noqa: F401
from test_worker_process_journey import execution, orders
from native_status_bridge import NativeStatusBridge
from qualification_runtime import private_file
from qualification_recovery_browser import observe_browser
import json

from sova.domain.contracts import ContractError
from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.adapters.postgres_governance import PostgresSecurityHolds
from sova.domain.contracts import ResourceScope
from sova.security.domain.governance import SecurityHold, HoldRelease
from datetime import timedelta
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
        bridge_token = private_file(tmp_path/'native-status-token',
            h.issuer.token(h.tenant, 'auditor')['authorization'].removeprefix('Bearer '))
        config = private_file(tmp_path/'native-status-config', json.dumps({
            'origin':h.origin, 'tenant_id':h.tenant, 'application_id':h.app,
            'unit_digest':unit, 'token_file':str(bridge_token)}))
        bridge = NativeStatusBridge(config)
        initial_view = bridge.state()
        assert initial_view['state']=='SOURCE_ACTIVE' and initial_view['recovery']['status']=='clear'
        assert initial_view['scope']['operation_id']==admission['operation_id']
        assert h.call('GET',f'{h.app_base}/units/{unit}',
            headers=h.issuer.token('another-tenant','auditor')).status_code==403
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
                    blocked_view = bridge.state()
                    assert blocked_view['state']=='SOURCE_FENCED'
                    assert blocked_view['recovery']['reason']=='OWNER_EXPIRED_UNSETTLED'
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
                # An unprivileged stats observer must not mistake hidden
                # sessions for proof of absence, even after actual settlement.
                limited_h = SimpleNamespace(tenant=h.tenant, app=h.app, clock=h.clock,
                    business={**h.business, 'dsn':make_conninfo(h.business['dsn'],user=postgres_database[1])})
                limited = NativeSettlement(limited_h,old,relay)
                lifecycle = PostgresLifecycleRepository(h.connect,tenant_id=h.tenant)
                assert limited.prove(lifecycle,h.tenant,h.app,admission['operation_id'],
                                     (copy[0]['digest'],)) is None
                assert not limited.issued and limited.observations==[{'remote_state':'unknown'}]
                observations['unprivileged_observer'] = limited.observations
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
                    target_counts = conn.execute('SELECT (SELECT count(*) FROM orders),'
                        '(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)').fetchone()
                with h.business['connect'](h.business['names']['src']) as conn:
                    source_counts = conn.execute('SELECT (SELECT count(*) FROM orders),'
                        '(SELECT count(*) FROM work_items),(SELECT count(*) FROM idempotency_results)').fetchone()
                    original = conn.execute("SELECT count(*) FROM orders o JOIN work_items w "
                        "ON w.id=o.id AND w.order_id=o.id AND w.state='done' "
                        "JOIN idempotency_results r ON r.customer=o.customer AND r.idempotency_key=o.idempotency_key "
                        "AND r.result='confirmed' WHERE o.id BETWEEN 1 AND 50 "
                        "AND o.customer='customer-'||(o.id%7)::text AND o.idempotency_key='key-'||o.id::text "
                        "AND o.amount=o.id*10").fetchone()[0]
                target_rows = target_counts[0]
                assert source_counts==(51,51,51) and original==50
                assert target_counts==((53,53,53) if mode=='committed' else (0,0,0))
                assert count==(1 if mode=='committed' else 0)
                assert target_rows==(53 if mode=='committed' else 0)
                final = execution(h)
                if mode=='uncommitted':
                    PostgresSecurityHolds(h.connect,tenant_id=h.tenant).open_hold(SecurityHold(
                        'settlement-security-review',ResourceScope(h.tenant,h.app,
                            h.world['source'].profile_id,'database'),'fixture-raiser',
                        'Fixture-only investigation after UNKNOWN',h.clock()-timedelta(seconds=1)))
                final_view = bridge.state()
                assert final_view['state']==('HANDOFF_COMPLETE' if mode=='committed' else 'SOURCE_FENCED')
                assert final_view['recovery']['status']==('clear' if mode=='committed' else 'required')
                assert final_view['recovery']['reason']==(None if mode=='committed' else 'EFFECT_RESULT_UNKNOWN')
                assert final_view['recovery']['authorizes_execution'] is False
                assert final_view['recovery']['security_holds']['active_count']==(mode=='uncommitted')
                browser = observe_browser(h, bridge,
                    {'state':final_view['state'], 'required':mode=='uncommitted',
                     'hold_count':int(mode=='uncommitted')}, tmp_path)
                # The next GET must fail after credential replacement. A prior
                # successful observation is never returned from a bridge cache.
                bridge_token.write_text(h.issuer.token('another-tenant','auditor')['authorization'].removeprefix('Bearer '))
                with pytest.raises(ContractError):
                    bridge.state()
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
                    committed_copy_audit=count, target_rows=target_rows, source_counts=source_counts,
                    target_counts=target_counts, original_records_verified=original, measurement=report,
                    migration_completed=(mode=='committed'), effect_apply_replayed=False)
                h.record('recovery-status-'+mode, initial=initial_view, blocked=blocked_view,
                    final=final_view, wrong_tenant_denied=True, credential_replacement_denied=True,
                    browser=browser,
                    local_native_api_and_bridge=True, production_deployed=False,
                    operational_proof_issued=False, automatic_replay_added=False)
            finally:
                if old is not None:
                    if old.process.poll() is None:
                        old.kill_owned_process()
                    old.stop()
                h.record('effect-settlement-progress-'+mode, observations=observations,
                    settlement_observations=settlement.observations if settlement else [],
                    successor_calls=calls, old_trace=old.events() if old else [],
                    progress_only=True, verification_completed=False)


def test_actual_governance_holds_and_release_reach_the_native_console(
        postgres_database, http_business, tmp_path):
    with runtime_service_journey(postgres_database,http_business,tmp_path,
                                  case_name='governance-held') as h:
        admission=h.approve_and_admit(h.register_forward(),'governance-approval')
        unit=admission['unit_digest']
        credential=private_file(tmp_path/'governance-token',h.issuer.token(h.tenant,'auditor')['authorization'].removeprefix('Bearer '))
        registration=private_file(tmp_path/'governance-config',json.dumps({'origin':h.origin,
            'tenant_id':h.tenant,'application_id':h.app,'unit_digest':unit,'token_file':str(credential)}))
        bridge=NativeStatusBridge(registration)
        original_history=h.status(unit)['history']
        holds=PostgresSecurityHolds(h.connect,tenant_id=h.tenant)
        source=h.world['source'].profile_id
        scope=ResourceScope(h.tenant,h.app,source,'database')
        raised=h.clock()-timedelta(seconds=1)
        # Other app/tenant incidents must not enter this application's snapshot.
        holds.open_hold(SecurityHold('other-app-hold',ResourceScope(h.tenant,h.app+'-other',source,'database'),
            'fixture-raiser','PRIVATE-DETAIL-NOT-EXPORTED',raised))
        other=PostgresSecurityHolds(h.connect,tenant_id=h.tenant+'-other')
        other.open_hold(SecurityHold('other-tenant-hold',ResourceScope(h.tenant+'-other',h.app,source,'database'),
            'fixture-raiser','PRIVATE-DETAIL-NOT-EXPORTED',raised))
        before=bridge.state()
        assert before['recovery']['status']=='clear'
        for hold_id in ('governance-1','governance-2'):
            holds.open_hold(SecurityHold(hold_id,scope,'fixture-raiser','PRIVATE-DETAIL-NOT-EXPORTED',raised))
        held=bridge.state()
        assert held['state']=='SOURCE_ACTIVE'
        assert held['recovery']['reason']=='SECURITY_HOLD'
        assert held['recovery']['security_holds']=={'active_count':2,
            'hold_ids':['governance-1','governance-2'],'operation_hold':False}
        assert held['recovery']['next_steps']==['REVIEW_SECURITY_HOLD']
        assert 'PRIVATE-DETAIL' not in json.dumps(held)
        held_browser=observe_browser(h,bridge,{'state':'SOURCE_ACTIVE','required':True,
            'reason_text':'보안 보류','hold_count':2},tmp_path)
        plan=PostgresLifecycleRepository(h.connect,tenant_id=h.tenant).get_plan(h.app,admission['plan_id'])
        partial=None
        for hold_id in ('governance-1','governance-2'):
            approved=h.clock()-timedelta(milliseconds=50)
            holds.release_hold(h.app,HoldRelease(hold_id,scope,plan.revisions,1,
                'fixture-requester','fixture-approver','fixture-only reviewed release',
                'fixture-evidence','a'*64,approved,approved+timedelta(minutes=1)))
            partial=bridge.state()
            if hold_id=='governance-1':
                assert partial['recovery']['security_holds']['hold_ids']==['governance-2']
                assert partial['recovery']['required'] is True
        cleared=partial
        assert cleared['recovery']['status']=='clear'
        assert cleared['recovery']['security_holds']['active_count']==0
        assert cleared['recovery']['next_steps']==[]
        h.case_name='governance-released'
        cleared_browser=observe_browser(h,bridge,{'state':'SOURCE_ACTIVE','required':False,
            'hold_count':0},tmp_path)
        # No worker ran and no authority/history transition was caused by reads.
        assert held['epoch']==cleared['epoch']==before['epoch']
        assert h.status(unit)['history']==original_history
        h.record('governance-status-lifecycle',before=before,held=held,cleared=cleared,
            held_browser=held_browser,cleared_browser=cleared_browser,
            cross_app_and_tenant_holds_excluded=True,private_details_excluded=True,
            worker_executed=False,synthetic_release_approval=True,production_deployed=False)
