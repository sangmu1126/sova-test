"""The observer preserves results/exceptions and refuses conflicting instrumentation."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from qualification_profile import StepProfiler
from qualification_timing import STEPS
from sova.domain.contracts import ContractError
from sova.service.worker import HandoffWorker


def test_no_arguments_results_or_sql_are_exported_and_original_result_is_preserved():
    p = StepProfiler()
    secret = 'postgresql://private:secret@private-host/sql-text'
    result = object()
    assert p.measure('stage_target', lambda _: result, secret) is result
    text = json.dumps(p.report({'scope': {}, 'authority_state': 'HANDOFF_COMPLETE'}))
    assert secret not in text and 'lambda' not in text and 'private-host' not in text
    assert not p.report({'scope': {}, 'authority_state': 'HANDOFF_COMPLETE'})['complete']


def test_original_exception_object_escapes_and_failed_step_remains_recorded():
    p = StepProfiler()
    failure = RuntimeError('private database failure details')
    def failing():
        raise failure
    with pytest.raises(RuntimeError) as got:
        p.measure('copy_content', failing)
    assert got.value is failure and not p.active and sys.getprofile() is None
    assert p.rows[0]['outcome'] == 'raised'
    assert 'private database' not in json.dumps(p.rows)


def test_instance_wrapper_is_removed_on_context_failure_without_changing_class_method():
    worker = HandoffWorker.__new__(HandoffWorker)
    original = HandoffWorker._step
    p = StepProfiler()
    failure = RuntimeError('abort')
    with pytest.raises(RuntimeError) as got:
        with p.worker(worker):
            assert '_step' in worker.__dict__
            raise failure
    assert got.value is failure and '_step' not in worker.__dict__
    assert worker._step.__func__ is original and HandoffWorker._step is original


def test_connection_factory_categories_export_code_counts_without_arguments_or_closures():
    secret = 'private-factory-dsn'
    def control():
        return secret
    def business():
        return secret
    p = StepProfiler(control_connect=control, business_connect=business)
    p.measure('copy_content', lambda: (control(), control(), business()))
    rows = {f['function']: f['calls'] for f in p.rows[0]['functions']}
    assert rows == {'factory.control.connect': 2, 'factory.business.admin': 1}
    assert secret not in json.dumps(p.rows) and 'business()' not in json.dumps(p.rows)
    with pytest.raises(ContractError):
        StepProfiler(control_connect=control, business_connect=control)


@pytest.mark.parametrize('invalid', ['unknown', 'another-thread', 'nested', 'existing-hook'])
def test_conflicting_or_unrecognized_observers_do_not_run_the_operation(invalid):
    p = StepProfiler()
    invoked = []
    def call():
        invoked.append(True)
    if invalid == 'another-thread':
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(ContractError):
                executor.submit(p.measure, 'stage_target', call).result()
    elif invalid == 'nested':
        with pytest.raises(ContractError):
            p.measure('stage_target', lambda: p.measure('stage_target', call))
    elif invalid == 'existing-hook':
        hook = lambda *args: None  # noqa: E731
        sys.setprofile(hook)
        try:
            with pytest.raises(ContractError):
                p.measure('stage_target', call)
            assert sys.getprofile() is hook
        finally:
            sys.setprofile(None)
    else:
        with pytest.raises(ContractError):
            p.measure(invalid, call)
    assert not invoked


@pytest.mark.parametrize('changed', ['missing', 'duplicate', 'failed', 'not-done'])
def test_incomplete_or_failed_profile_is_not_reported_as_complete(changed):
    p = StepProfiler()
    for step in STEPS:
        p.measure(step, lambda: None)
    ledger = {'scope': {'plan_id': 'plan'}, 'authority_state': 'HANDOFF_COMPLETE'}
    assert p.report(ledger)['complete']
    if changed == 'missing':
        p.rows.pop()
    elif changed == 'duplicate':
        p.rows.append(p.rows[-1])
    elif changed == 'failed':
        p.rows[0]['outcome'] = 'raised'
    else:
        ledger['authority_state'] = 'FAILBACK_REQUIRED'
    report = p.report(ledger)
    assert not report['complete'] and not report['authorizes_execution'] and not report['qualification_issued']
