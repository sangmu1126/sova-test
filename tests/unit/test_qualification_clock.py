"""Clock evidence must retain failures without disclosing connection material."""

from datetime import UTC, datetime, timedelta
import json

import psycopg
import pytest
from qualification_postgres import ClockWatch


SECRET = "password=private-password host=private-host token=private-token /private/client-key.pem"


class Connection:
    def __init__(self, *, query_error=None, close_error=None, offset=0):
        self.query_error, self.close_error, self.offset = query_error, close_error, offset

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.close_error:
            raise self.close_error

    def execute(self, _):
        if self.query_error:
            raise self.query_error
        return self

    def fetchone(self):
        return (datetime.now(UTC) + timedelta(seconds=self.offset),)


class Cadence:
    """Advance sampling points deterministically, without retrying a point."""
    def __init__(self, samples):
        self.remaining, self.intervals, self.stopped = samples, [], False

    def wait(self, interval):
        self.intervals.append(interval)
        if self.stopped or self.remaining == 0:
            return True
        self.remaining -= 1
        return False

    def set(self):
        self.stopped = True


@pytest.mark.parametrize("stage,error", [
    ("connect", psycopg.errors.ConnectionTimeout(SECRET)),
    ("query", psycopg.errors.QueryCanceled(SECRET)),
    ("close", psycopg.OperationalError(SECRET)),
])
def test_failures_identify_phase_stage_and_driver_type_without_messages(monkeypatch, stage, error):
    calls = []
    def connect(_):
        calls.append(True)
        if stage == "connect":
            raise error
        return Connection(**{stage + "_error": error})
    monkeypatch.setattr(psycopg, "connect", connect)
    watch = ClockWatch(SECRET)
    with pytest.raises(type(error)):
        watch.sample("periodic")
    assert len(calls) == 1 and len(watch.samples) == 1
    record = watch.samples[0]
    assert record["error"] == "clock_sample_failed"
    assert record["phase"] == "periodic" and record["stage"] == stage
    assert record["exception_class"] == type(error).__name__
    assert record["sqlstate"] == ("57014" if stage == "query" else None)
    assert record["elapsed_ms"] >= 0
    assert datetime.fromisoformat(record["host_after"]) >= datetime.fromisoformat(record["host_before"])
    assert set(record) == {"error", "phase", "stage", "host_before", "host_after", "elapsed_ms", "exception_class", "sqlstate"}
    assert not any(part in json.dumps(record) for part in [SECRET, "private-password", "private-host", "private-token", "client-key.pem"])


def test_start_failure_is_retained_after_a_distinct_successful_final_observation(monkeypatch):
    calls = []
    def connect(_):
        calls.append(True)
        if len(calls) == 1:
            raise psycopg.errors.ConnectionTimeout(SECRET)
        return Connection()
    monkeypatch.setattr(psycopg, "connect", connect)
    watch = ClockWatch(SECRET)
    with pytest.raises(psycopg.errors.ConnectionTimeout):
        watch.start()
    assert len(calls) == 1 and not watch.thread.is_alive()
    report = watch.stop()
    assert not report["passed"] and report["sampling_failure_count"] == 1
    assert [s["phase"] for s in report["samples"]] == ["start", "stop"]


def test_final_observation_failure_returns_failed_evidence_instead_of_losing_report(monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda _: Connection())
    watch = ClockWatch(SECRET)
    watch.sample("start")
    def fail(_):
        raise psycopg.OperationalError(SECRET)
    monkeypatch.setattr(psycopg, "connect", fail)
    report = watch.stop()
    assert report["thread_stopped"] and not report["passed"]
    assert report["sampling_failure_count"] == 1 and report["samples"][-1]["phase"] == "stop"
    assert SECRET not in json.dumps(report)


def test_next_periodic_success_does_not_erase_previous_failure_or_retry_that_point(monkeypatch):
    calls = []
    def connect(_):
        calls.append(True)
        if len(calls) == 1:
            raise psycopg.OperationalError(SECRET)
        return Connection()
    monkeypatch.setattr(psycopg, "connect", connect)
    watch = ClockWatch(SECRET)
    watch.done = Cadence(2)
    watch.watch()
    assert len(calls) == 2 and watch.done.intervals == [0.5, 0.5, 0.5]
    report = watch.stop()
    assert not report["passed"] and report["sampling_failure_count"] == 1
    assert len(report["samples"]) == 3


def test_unexpected_background_failure_cannot_silently_leave_passing_history(monkeypatch):
    # A custom name and message are untrusted diagnostic values too.
    error_type = type("private-token", (Exception,), {})
    def fail(_):
        raise error_type(SECRET)
    monkeypatch.setattr(psycopg, "connect", fail)
    watch = ClockWatch(SECRET)
    watch.done = Cadence(5)
    watch.watch()
    assert watch.done.stopped and len(watch.samples) == 1
    assert watch.samples[0]["exception_class"] == "UnexpectedSamplerError"
    monkeypatch.setattr(psycopg, "connect", lambda _: Connection())
    report = watch.stop()
    assert not report["passed"] and report["sampling_failure_count"] == 1
    assert not any(x in json.dumps(report) for x in [SECRET, "private-token"])


def test_unjoined_sampler_is_incomplete_and_does_not_start_a_concurrent_final_query(monkeypatch):
    class Stuck:
        def is_alive(self):
            return True
        def join(self, timeout):
            assert timeout == 5
    def forbidden(_):
        pytest.fail("A still-running sampler must not launch a concurrent final query")
    monkeypatch.setattr(psycopg, "connect", forbidden)
    watch = ClockWatch(SECRET)
    watch.thread = Stuck()
    report = watch.stop()
    assert not report["thread_stopped"] and not report["passed"]
    assert report["samples"][-1]["error"] == "clock_sampler_still_running"


def test_measured_skew_above_existing_one_second_limit_still_blocks_start(monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda _: Connection(offset=10))
    watch = ClockWatch(SECRET)
    with pytest.raises(RuntimeError, match="more than one second"):
        watch.start()
    report = watch.stop()
    assert not report["passed"] and report["max_allowed_skew_seconds"] == 1
    assert report["sampling_failure_count"] == 0


def test_unknown_psycopg_subclass_name_and_sqlstate_are_not_diagnostic_authority(monkeypatch):
    error_type = type("private-token", (psycopg.Error,), {"sqlstate": "SECRET"})
    def fail(_):
        raise error_type(SECRET)
    monkeypatch.setattr(psycopg, "connect", fail)
    watch = ClockWatch(SECRET)
    with pytest.raises(psycopg.Error):
        watch.sample()
    record = watch.samples[-1]
    assert record["exception_class"] == "PsycopgError" and record["sqlstate"] is None
    assert "private-token" not in json.dumps(record)
