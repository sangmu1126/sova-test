"""Actual native worker steps over TLS PG; profiler timing is not Pod/client timing."""

from contextlib import nullcontext
from time import perf_counter_ns

import pytest
from qualification_profile import StepProfiler
from qualification_service import service_journey
from qualification_timing import STEPS, read_ledger
from test_order_http_handoff import http_business as http_business  # noqa: F401

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


@pytest.mark.parametrize('mode', ['plain-before', 'profiled', 'plain-after'])
def test_native_worker_profile_preserves_plan_checks_and_real_data(postgres_database, http_business, tmp_path, mode):
    with service_journey(postgres_database, http_business, tmp_path) as h:
        admission = h.approve_and_admit(h.register_forward(), 'profile-approval')
        worker, p = h.worker(), StepProfiler()
        before = worker._step.__func__
        started = perf_counter_ns()
        try:
            with p.worker(worker) if mode == 'profiled' else nullcontext():
                result = worker.drive(h.tenant, h.app, admission['unit_digest'])
        finally:
            wall = (perf_counter_ns() - started) / 1_000_000
            # Preserve raw observations even if the drive or later assertions fail.
            # This file contains no claim that SQL verification has succeeded.
            h.record('worker-step-' + mode + '-raw', mode=mode, admission=admission,
                     worker_wall_ms=round(wall, 3), steps=p.rows, verification_completed=False)
        assert worker._step.__func__ is before and '_step' not in worker.__dict__
        assert result.state.value == 'HANDOFF_COMPLETE'
        ledger = read_ledger(h.connect, h.tenant, h.app, admission)
        assert len(ledger['effects']) == 21 and len(ledger['authority']) == 10
        assert len(ledger['measurement_receipts']) == 16
        assert all(r['result'] == 'pass' for r in ledger['measurement_receipts'])
        assert h.route.get() == h.business['names']['tgt']
        with h.business['connect'](h.route.get()) as conn:
            assert conn.execute('SELECT count(*) FROM orders').fetchone() == (50,)
            assert conn.execute('SELECT count(*) FROM work_items').fetchone() == (50,)
            assert conn.execute('SELECT count(*) FROM idempotency_results').fetchone() == (50,)
        report = p.report(ledger)
        if mode == 'profiled':
            assert report['complete'] and [s['step'] for s in report['steps']] == list(STEPS)
            functions = [f for s in report['steps'] for f in s['functions']]
            assert all(f['calls'] > 0 and f['inclusive_ms'] >= f['self_ms'] >= 0 for f in functions)
            assert {'db.connect', 'db.cursor.execute', 'db.transaction.enter', 'db.transaction.exit',
                    'control.evidence.prove', 'control.verifier.resolve', 'control.authority.advance',
                    'probe.restore_drill', 'probe.content_equal', 'probe.single_writer'} <= {f['function'] for f in functions}
        else:
            assert not report['steps'] and not report['complete']
        h.record('worker-step-' + mode, mode=mode, worker_wall_ms=round(wall, 3), profile=report,
                 ledger=ledger, historical_rows=50, client_load=False, connections_reused=False)
