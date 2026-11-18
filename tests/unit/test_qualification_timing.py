"""Timing summaries cannot fill missing data, switch clocks or hide failure."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

import pytest
from qualification_timing import ACTIONS, analyze, client_scope

from sova.domain.contracts import ContractError
from sova.qualification.requests import RequestJournal


@pytest.fixture
def observations():
    at = datetime(2026, 11, 19, tzinfo=UTC)
    effects = []
    for i, action in enumerate(ACTIONS):
        for revision, status, offset in [(1, 'prepared', 0), (2, 'in_flight', 10), (3, 'confirmed_applied', 30)]:
            effects.append({'action': action, 'fingerprint': str(i)*64, 'revision': revision, 'status': status,
                            'at': (at+timedelta(seconds=i, milliseconds=offset)).isoformat()})
    ledger = {'scope': {'tenant_id': 'tenant'}, 'effects': effects, 'authority_state': 'HANDOFF_COMPLETE',
        'snapshot': {'read_only': True, 'isolation': 'repeatable read'},
        'authority': [{'revision': i+1, 'state': 'HANDOFF_COMPLETE' if i == 9 else 'SOURCE_ACTIVE',
                       'next_operation': i, 'at': (at+timedelta(seconds=i)).isoformat()} for i in range(10)]}
    client = {'stop_received': True, 'bounded_results': [{'request_id': 'request-'+str(i), 'result': 'CONFIRMED',
              'reason': 'validated_receipt', 'elapsed_ms': 20} for i in range(100)]}
    events = [{'request_id': 'request-'+str(i), 'phase': 'load', 'status': 200} for i in range(100)]
    return ledger, client, events


def test_partition_uses_only_db_events_and_retains_client_exhaustion(observations):
    ledger, client, events = observations
    client['bounded_results'][0].update(result='EXHAUSTED', reason='deadline', elapsed_ms=4400)
    events[0]['status'] = 503
    result = analyze(ledger, client, events)
    assert result['pause_prepare_to_route_confirmation_ms'] == 5030
    assert result['partition_ms'] == {'reserve_to_dispatch': 60, 'dispatch_to_confirmation': 120, 'between_effects': 4850}
    assert result['client']['bounded_results'] == {'CONFIRMED': 99, 'EXHAUSTED': 1}
    assert not result['client']['all_confirmed'] and not result['qualification_issued'] and not result['authorizes_execution']
    assert not result['time_domains']['cross_clock_subtraction_used']


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'unknown', 'reversed', 'naive', 'incomplete_history', 'not_readonly', 'not_complete'])
def test_incomplete_or_ambiguous_ledger_never_gets_a_positive_timing_summary(observations, change):
    ledger, client, events = observations
    if change == 'missing':
        ledger['effects'].pop()
    elif change == 'duplicate':
        ledger['effects'][-1] = deepcopy(ledger['effects'][-2])
    elif change == 'unknown':
        ledger['effects'][-1]['status'] = 'unknown'
    elif change == 'reversed':
        ledger['effects'][5]['at'] = ledger['effects'][0]['at']
    elif change == 'naive':
        ledger['effects'][0]['at'] = '2026-11-19T00:00:00'
    elif change == 'incomplete_history':
        ledger['authority'].pop()
    elif change == 'not_readonly':
        ledger['snapshot']['read_only'] = False
    else:
        ledger['authority_state'] = 'FAILBACK_REQUIRED'
    with pytest.raises(ContractError):
        analyze(ledger, client, events)


@pytest.mark.parametrize('change', ['duplicate', 'missing', 'nan', 'no_stop'])
def test_partial_client_data_cannot_be_treated_as_completed_measurement(observations, change):
    ledger, client, events = observations
    if change == 'duplicate':
        client['bounded_results'][-1]['request_id'] = client['bounded_results'][0]['request_id']
    elif change == 'missing':
        events.pop()
    elif change == 'nan':
        client['bounded_results'][-1]['elapsed_ms'] = float('nan')
    else:
        client['stop_received'] = False
    with pytest.raises(ContractError):
        analyze(ledger, client, events)


def test_client_journal_scope_is_read_only_and_binds_exact_run_tenant_and_profile(tmp_path):
    p = tmp_path / 'client.sqlite'
    journal = RequestJournal.create(p, run_id='run-a', tenant_id='tenant', profile_digest='a'*64)
    expected = dict(journal.scope)
    journal.close()
    before = p.read_bytes()
    assert client_scope(p, expected, 'tenant') == expected
    assert p.read_bytes() == before
    for changed in [{**expected, 'run_id': 'run-b'}, {**expected, 'tenant_id': 'another'},
                    {**expected, 'profile_digest': 'b'*64}]:
        with pytest.raises(ContractError):
            client_scope(p, changed, 'tenant')
    assert 'another' not in json.dumps(client_scope(p, expected, 'tenant'))
