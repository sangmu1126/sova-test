"""Qualification-only current-thread profiling; no SQL, arguments or raw stats export.

Inclusive timings overlap. Profiling adds overhead and excludes keeper/API threads.
This observer never changes connections, transaction boundaries or trust checks.
"""

import cProfile
from contextlib import contextmanager
import inspect
import sys
import threading
import time

import psycopg

from qualification_timing import STEPS
from sova.adapters.ed25519_evidence import Ed25519EvidenceSigner
from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.adapters.postgres_handoff_probes import PostgresHandoffProbes
from sova.adapters.postgres_verifier_registry import PostgresVerifierRegistry
from sova.application.handoff_coordinator import HandoffCoordinator
from sova.application.signed_evidence import SignedEvidenceAuthority
from sova.domain.contracts import ContractError
from sova.service.worker import HandoffWorker


def selected_functions():
    functions = {
        'db.connect': psycopg.Connection.connect.__func__,
        'db.cursor.execute': psycopg.Cursor.execute,
        'db.transaction.enter': psycopg.Transaction.__enter__,
        'db.transaction.exit': psycopg.Transaction.__exit__,
        'db.connection.commit': psycopg.Connection.commit,
        'db.connection.rollback': psycopg.Connection.rollback,
        'db.connection.close': psycopg.Connection.close,
        'control.evidence.prove': HandoffWorker._prove,
        'control.evidence.ingest': SignedEvidenceAuthority.ingest,
        'control.evidence.sign': Ed25519EvidenceSigner.sign,
        'control.verifier.resolve': PostgresVerifierRegistry.resolve,
        'control.authority.advance': HandoffCoordinator.advance_from_ledger,
        'control.witness.append': FileAuthorityWitness.append,
        'control.witness.latest': FileAuthorityWitness.latest,
    }
    for name in ('zone', 'write_denied', 'content_equal', 'order_effects_once',
                 'cross_tenant_denied', 'transport_encrypted', 'restore_drill', 'single_writer'):
        functions['probe.' + name] = getattr(PostgresHandoffProbes, name)
    return {function.__code__: label for label, function in functions.items()}


class StepProfiler:
    def __init__(self, *, control_connect=None, business_connect=None, business_connect_as=None):
        self.rows = []
        self.owner = threading.get_ident()
        self.active = False
        self.selected = selected_functions()
        for label, function in [('factory.control.connect', control_connect),
                                ('factory.business.admin', business_connect),
                                ('factory.business.role', business_connect_as)]:
            if function is None:
                continue
            if not inspect.isfunction(function) or inspect.isgeneratorfunction(function) \
                    or inspect.iscoroutinefunction(function) or function.__code__ in self.selected:
                raise ContractError('Distinct synchronous connection factory code required')
            # Only the code identity and fixed category are retained, never closures/DSNs.
            self.selected[function.__code__] = label

    def measure(self, kind, function, *args, **kwargs):
        if kind not in STEPS or self.owner != threading.get_ident() or self.active or sys.getprofile() is not None:
            raise ContractError('Profiling requires one known step in an unprofiled owner thread')
        profile = cProfile.Profile()
        outcome = 'raised'
        self.active = True
        started = time.perf_counter_ns()
        try:
            result = profile.runcall(function, *args, **kwargs)
            outcome = 'returned'
            return result
        finally:
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            self.active = False
            # Never dump raw pstats: they include paths and unselected symbols.
            functions = []
            for entry in profile.getstats():
                if entry.code in self.selected:
                    functions.append({'function': self.selected[entry.code],
                        'calls': entry.callcount, 'recursive_calls': entry.reccallcount,
                        'self_ms': round(entry.inlinetime * 1000, 3),
                        'inclusive_ms': round(entry.totaltime * 1000, 3)})
            self.rows.append({'step': kind, 'outcome': outcome, 'wall_ms': round(elapsed, 3),
                              'functions': sorted(functions, key=lambda r: r['function'])})

    @contextmanager
    def worker(self, worker):
        # Instance-local wrapper only; no global patching, subclass or other thread.
        if type(worker) is not HandoffWorker or '_step' in worker.__dict__ or self.rows or self.active \
                or self.owner != threading.get_ident() or sys.getprofile() is not None:
            raise ContractError('An unmodified native worker and fresh profiler are required')
        original = worker._step
        signature = inspect.signature(original)

        def measured(*args, **kwargs):
            op = signature.bind(*args, **kwargs).arguments['op']
            return self.measure(op['kind'], original, *args, **kwargs)

        worker._step = measured
        try:
            yield self
        finally:
            if worker.__dict__.get('_step') is measured:
                del worker._step

    def report(self, ledger):
        complete = (ledger['authority_state'] == 'HANDOFF_COMPLETE'
                    and [r['step'] for r in self.rows] == list(STEPS)
                    and all(r['outcome'] == 'returned' for r in self.rows))
        return {'version': 1, 'scope': ledger['scope'], 'complete': complete, 'steps': self.rows,
            'measurement': 'current-thread cProfile function timings plus perf_counter step wall time',
            'inclusive_functions_overlap': True, 'profiling_overhead_removed': False,
            'background_threads_profiled': False, 'client_latency_measured': False,
            'connections_reused': False, 'qualification_issued': False, 'authorizes_execution': False}
