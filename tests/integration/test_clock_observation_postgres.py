"""Actual TLS observation/refusal and an explicitly injected real SQL error."""

from contextlib import contextmanager
import json

import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from qualification_postgres import ClockWatch

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_tls]


def test_actual_tls_database_clock_start_periodic_and_stop_are_observed(postgres_database):
    watch = ClockWatch(postgres_database[0])
    try:
        watch.start()
        watch.sample()
    finally:
        report = watch.stop()
    assert report["passed"] and report["thread_stopped"] and report["sampling_failure_count"] == 0
    assert {x["phase"] for x in report["samples"]} == {"start", "periodic", "stop"}


def test_actual_wrong_tls_hostname_is_retained_as_connection_failure(postgres_database):
    dsn = make_conninfo(postgres_database[0], host="wrong-clock.fixture", hostaddr="127.0.0.1")
    watch = ClockWatch(dsn)
    with pytest.raises(psycopg.OperationalError):
        watch.start()
    report = watch.stop()
    assert not report["passed"] and report["sampling_failure_count"] == 2
    assert {r["phase"] for r in report["samples"]} == {"start", "stop"}
    assert all(r["stage"] == "connect" and r["exception_class"] == "OperationalError" for r in report["samples"])
    assert "wrong-clock.fixture" not in json.dumps(report) and dsn not in json.dumps(report)


def test_actual_injected_sql_error_keeps_sqlstate_and_does_not_become_a_clock_skew(postgres_database, monkeypatch):
    # This deliberately changes the test connection's query, not the runner or
    # product query. It is not a reproduction of the historical unknown failure.
    connect = psycopg.connect
    @contextmanager
    def injected(dsn):
        with connect(dsn) as connection:
            class QueryFailure:
                def execute(self, _):
                    return connection.execute("SELECT 1/0")
            yield QueryFailure()
    watch = ClockWatch(postgres_database[0])
    monkeypatch.setattr(psycopg, "connect", injected)
    with pytest.raises(psycopg.errors.DivisionByZero):
        watch.sample()
    monkeypatch.setattr(psycopg, "connect", connect)
    report = watch.stop()
    assert not report["passed"] and report["sampling_failure_count"] == 1
    failed = report["samples"][0]
    assert failed["stage"] == "query" and failed["exception_class"] == "DivisionByZero"
    assert failed["sqlstate"] == "22012" and "skew_lower_bound_seconds" not in failed
    assert "division by zero" not in json.dumps(report)
