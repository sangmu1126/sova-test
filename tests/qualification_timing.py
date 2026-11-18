"""Read-only, plan-bound timing for the local offline-copy qualification.

No observation bodies, approval documents, tokens or command parameters leave
the database. This diagnostic snapshot never authenticates a gate or issues
an execution/availability qualification.
"""

from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
import sqlite3

import psycopg
from psycopg.types.json import Jsonb

from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.domain.contracts import ContractError, digest, identifier


STEPS = ('stage_target', 'pause_admission', 'fence_source_writes', 'copy_content',
         'verify_final_state', 'record_activation_intent', 'activate_target', 'commit_route', 'stabilize')
ACTIONS = tuple(s for s in STEPS if s not in ('record_activation_intent', 'stabilize'))
PROBES = {'zone', 'write_denied', 'content_equal', 'order_effects_once', 'cross_tenant_denied',
          'transport_encrypted', 'restore_drill', 'single_writer', 'measured_budget'}
LIMIT = 256


def read_ledger(connect, tenant, app, admission):
    identifier(tenant)
    identifier(app)
    if admission.get('application_id') != app:
        raise ContractError('Timing admission application mismatch')
    unit, operation, plan_id = admission['unit_digest'], admission['operation_id'], admission['plan_id']
    digest(unit)
    identifier(operation)
    identifier(plan_id)
    def readonly():
        conn = connect()
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        return conn
    repo = PostgresLifecycleRepository(readonly, tenant_id=tenant)
    with repo.transaction() as conn:
        row = conn.execute(
            "SELECT a.plan_digest,a.compiled_digest,a.state,p.document->'revisions' "
            "FROM sova_control.handoff_authority a JOIN sova_control.operations o "
            "ON (a.tenant_id,a.application_id,a.plan_digest)=(o.tenant_id,o.application_id,o.plan_digest) "
            "JOIN sova_control.plans p ON (p.tenant_id,p.application_id,p.plan_id,p.digest)="
            "(o.tenant_id,o.application_id,o.plan_id,o.plan_digest) "
            "WHERE a.tenant_id=%s AND a.application_id=%s AND a.unit_digest=%s "
            "AND o.operation_id=%s AND o.plan_id=%s", (tenant, app, unit, operation, plan_id)).fetchall()
        if len(row) != 1:
            raise ContractError('Timing admission scope mismatch')
        plan, compiled, state, revisions = row[0]
        canonical = conn.execute(
            'SELECT canonical FROM sova_control.compiled_plans WHERE tenant_id=%s AND application_id=%s AND compiled_digest=%s',
            (tenant, app, compiled)).fetchone()
        if not canonical or hashlib.sha256(canonical[0].encode()).hexdigest() != compiled:
            raise ContractError('Timing compiled plan integrity mismatch')
        doc = json.loads(canonical[0])
        if (doc['tenant_id'], doc['service_id'], doc['unit']['unit_digest']) != (tenant, app, unit) \
                or tuple(o['kind'] for o in doc['operations']) != STEPS:
            raise ContractError('Unsupported timing plan or scope')
        step_ids = {o['operation_id']: o['kind'] for o in doc['operations']}
        due = {o['id']: step_ids[o['required_at']] for o in doc['obligations']}
        events = conn.execute(
            "SELECT e.command_document->>'action',e.fingerprint,e.command_document->>'plan_digest',"
            "v.revision,v.status,v.recorded_at FROM sova_control.effects e "
            "JOIN sova_control.effect_events v USING(tenant_id,application_id,idempotency_key) "
            "WHERE e.tenant_id=%s AND e.application_id=%s AND e.operation_id=%s "
            'ORDER BY v.recorded_at,v.revision LIMIT 257', (tenant, app, operation)).fetchall()
        authority = conn.execute(
            "SELECT revision,document->>'state',document->>'next_operation',recorded_at "
            "FROM sova_control.handoff_authority_events WHERE tenant_id=%s AND application_id=%s "
            'AND unit_digest=%s AND plan_digest=%s ORDER BY revision LIMIT 257',
            (tenant, app, unit, plan)).fetchall()
        receipts = conn.execute(
            "SELECT s.document #>> '{evidence,obligation_id}',o.artifact_digest,(o.document::jsonb)->>'probe',s.stored_at,"
            "s.document #>> '{evidence,result}' FROM sova_control.signed_security_evidence s "
            "JOIN sova_control.evidence_observations o ON (s.tenant_id,s.application_id)=(o.tenant_id,o.application_id) "
            "AND s.document #>> '{evidence,artifact_digest}'=o.artifact_digest "
            "WHERE s.tenant_id=%s AND s.application_id=%s AND s.document #> '{evidence,revisions}'=%s "
            'ORDER BY s.stored_at LIMIT 257', (tenant, app, Jsonb(revisions))).fetchall()
        if any(len(x) > LIMIT for x in (events, authority, receipts)):
            raise ContractError('Timing observation exceeds bounded export')
        if any(action not in ACTIONS or bound != plan for action, _, bound, *_ in events):
            raise ContractError('Timing effect binding mismatch')
        if any(oid not in due or probe not in PROBES or result not in ('pass', 'fail', 'unknown')
               for oid, _, probe, _, result in receipts):
            raise ContractError('Timing receipt metadata mismatch')
        at, snapshot, isolation, ro = conn.execute(
            "SELECT clock_timestamp(),pg_current_snapshot()::text,current_setting('transaction_isolation'),"
            "current_setting('transaction_read_only')").fetchone()
    return {'version': 1, 'scope': {'tenant_id': tenant, 'application_id': app, 'unit_digest': unit,
                'operation_id': operation, 'plan_id': plan_id, 'plan_digest': plan, 'compiled_digest': compiled},
        'snapshot': {'observed_at': at.isoformat(), 'id': snapshot, 'isolation': isolation, 'read_only': ro == 'on'},
        'authority_state': state,
        'effects': [{'action': a, 'fingerprint': f, 'revision': rev, 'status': status, 'at': t.isoformat()}
                    for a, f, _, rev, status, t in events],
        'authority': [{'revision': rev, 'state': s, 'next_operation': int(n), 'at': t.isoformat()}
                      for rev, s, n, t in authority],
        'measurement_receipts': [{'obligation_id': oid, 'artifact_digest': dig, 'probe': probe,
                                  'required_at': due[oid], 'stored_at': t.isoformat(), 'result': result}
                                 for oid, dig, probe, t, result in receipts],
        'signature_authenticated_by_this_export': False, 'qualification_issued': False, 'authorizes_execution': False}


def instant(value):
    try:
        at = datetime.fromisoformat(value)
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError()
        return at
    except (ValueError, TypeError):
        raise ContractError('Invalid timing timestamp') from None


def ms(start, end):
    value = (instant(end) - instant(start)).total_seconds() * 1000
    if value < 0 or not math.isfinite(value):
        raise ContractError('Reversed or nonfinite timing interval')
    return round(value, 3)


def analyze(ledger, client, events):
    if ledger['authority_state'] != 'HANDOFF_COMPLETE' or not ledger['snapshot']['read_only'] \
            or ledger['snapshot']['isolation'] != 'repeatable read':
        raise ContractError('Incomplete timing authority or snapshot')
    if len(ledger['effects']) != 21 or set(x['action'] for x in ledger['effects']) != set(ACTIONS):
        raise ContractError('Incomplete timing effect set')
    history = ledger['authority']
    if [x['revision'] for x in history] != list(range(1, 11)) or history[-1]['state'] != 'HANDOFF_COMPLETE' \
            or history[-1]['next_operation'] != 9:
        raise ContractError('Incomplete timing authority history')
    for left, right in zip(history, history[1:]):
        ms(left['at'], right['at'])
    phases, previous = [], None
    for action in ACTIONS:
        rows = [x for x in ledger['effects'] if x['action'] == action]
        if len(rows) != 3 or [(r['revision'], r['status']) for r in rows] != [
                (1, 'prepared'), (2, 'in_flight'), (3, 'confirmed_applied')] \
                or len({r['fingerprint'] for r in rows}) != 1:
            raise ContractError('Incomplete or ambiguous timing effect')
        prepared, dispatched, confirmed = (r['at'] for r in rows)
        phases.append({'action': action, 'prepared_at': prepared, 'dispatched_at': dispatched,
            'confirmed_at': confirmed, 'reserve_to_dispatch_ms': ms(prepared, dispatched),
            'dispatch_to_confirmation_ms': ms(dispatched, confirmed),
            'previous_confirmation_to_prepare_ms': ms(previous, prepared) if previous else None})
        previous = confirmed
    window = phases[1:]
    duration = ms(window[0]['prepared_at'], window[-1]['confirmed_at'])
    reserves = sum(p['reserve_to_dispatch_ms'] for p in window)
    envelopes = sum(p['dispatch_to_confirmation_ms'] for p in window)
    gaps = sum(p['previous_confirmation_to_prepare_ms'] for p in window[1:])
    if abs(duration - reserves - envelopes - gaps) > .02:
        raise ContractError('Timing partition mismatch')
    if not client.get('stop_received') or len(client['bounded_results']) != 100 or sum(e['phase'] == 'load' for e in events) != 100:
        raise ContractError('Incomplete client timing observations')
    initial = [e for e in events if e['phase'] == 'load']
    ids = [e['request_id'] for e in initial]
    bounded = client['bounded_results']
    if len(set(ids)) != 100 or {x['request_id'] for x in bounded} != set(ids) \
            or len({x['request_id'] for x in bounded}) != 100:
        raise ContractError('Ambiguous client timing identities')
    for x in bounded:
        if x['result'] not in ('CONFIRMED', 'EXHAUSTED', 'REJECTED') \
                or type(x['elapsed_ms']) not in (int, float) or not math.isfinite(x['elapsed_ms']) or x['elapsed_ms'] < 0:
            raise ContractError('Invalid client timing result')
    counts = Counter(x['result'] for x in client['bounded_results'])
    return {'complete': True, 'scope': ledger['scope'], 'phases': phases,
        'pause_prepare_to_route_confirmation_ms': duration,
        'partition_ms': {'reserve_to_dispatch': round(reserves, 3),
                         'dispatch_to_confirmation': round(envelopes, 3), 'between_effects': round(gaps, 3)},
        'largest_between_effects': sorted(window[1:], key=lambda p: p['previous_confirmation_to_prepare_ms'], reverse=True),
        'client': {'bounded_results': dict(counts),
            'exhaustion_reasons': dict(Counter(x['reason'] for x in client['bounded_results'] if x['result'] != 'CONFIRMED')),
            'initial_failures': sum(e['status'] != 200 for e in initial),
            'maximum_inline_elapsed_ms': max(x['elapsed_ms'] for x in client['bounded_results']),
            'all_confirmed': counts == {'CONFIRMED': 100}},
        'time_domains': {'phase_durations': 'database clock_timestamp event records, not commit timestamps',
            'client_elapsed': 'client monotonic clock', 'cross_clock_subtraction_used': False},
        'limits': ['dispatch envelope includes authorization/provider/readback/bookkeeping, not pure provider time',
            'between-effect interval includes verification/evidence/authority/lease work, not a measured single probe duration',
            'this local offline-copy timing export neither authenticates evidence nor qualifies service continuity'],
        'qualification_issued': False, 'authorizes_execution': False}


def client_scope(path, expected, tenant):
    if path.is_symlink() or not path.is_file():
        raise ContractError('Timing journal must be a regular existing file')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT document FROM metadata').fetchall()
    if len(rows) != 1:
        raise ContractError('Invalid timing journal scope')
    scope = json.loads(rows[0][0])
    if set(scope) != {'version', 'run_id', 'tenant_id', 'profile_digest'} or type(scope['version']) is not int \
            or scope['version'] != 1 or scope != expected or scope['tenant_id'] != tenant:
        raise ContractError('Client timing scope mismatch')
    identifier(scope['run_id'])
    digest(scope['profile_digest'])
    return scope


def capture_timing(h, env, admission, traffic):
    if 'cutover_timing' in env.report:
        return
    try:
        ledger = read_ledger(h.connect, h.tenant, h.app, admission)
        with (env.directory / 'cutover-ledger.json').open('x') as file:
            file.write(json.dumps(ledger, indent=2) + '\n')
        scope = client_scope(traffic.results / 'client.sqlite', traffic.scope, h.tenant)
        if json.loads((traffic.results / 'reconciliation.json').read_text())['scope'] != scope:
            raise ContractError('Timing reconciliation scope mismatch')
        client = json.loads((traffic.results / 'client.json').read_text())
        if client['mode'] != traffic.mode:
            raise ContractError('Timing client mode mismatch')
        report = analyze(ledger, client, traffic.events())
        report['client_scope'] = scope
        with (env.directory / 'cutover-timing.json').open('x') as file:
            file.write(json.dumps(report, indent=2) + '\n')
        env.report['cutover_timing'] = report
    except Exception as error:
        env.report['cutover_timing'] = {'complete': False, 'error_class': type(error).__name__,
            'qualification_issued': False, 'authorizes_execution': False}
    env.save()
