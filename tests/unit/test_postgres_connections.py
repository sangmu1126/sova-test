"""Reused administrative connections behave like fresh ones (on doubles)."""

import pytest

from sova.adapters.postgres_connections import ReusedConnections
from psycopg import pq


class Conn:
    def __init__(self, n):
        self.n, self.closed, self.broken, self.autocommit = n, False, False, False
        self.statements, self.commits, self.rollbacks = [], 0, 0
        self.info = type("Info", (), {"transaction_status": pq.TransactionStatus.IDLE})()

    def execute(self, statement):
        self.statements.append((statement, self.autocommit))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def pool(clock=None):
    made = []

    def factory(database):
        made.append(Conn(len(made)))
        return made[-1]

    return ReusedConnections(factory, max_idle_seconds=30, clock=clock or Clock()), made


def test_a_connection_is_reused_and_reset_between_uses():
    connections, made = pool()
    with connections("db") as first:
        pass
    with connections("db") as second:
        pass
    assert first is second and len(made) == 1
    assert ("DISCARD ALL", True) in first.statements  # reset outside a transaction
    assert first.autocommit is False and first.commits == 2
    # The reset drops prepared statements under the driver: none are auto-prepared.
    assert first.prepare_threshold is None


def test_an_error_rolls_back_and_a_broken_session_is_dropped():
    connections, made = pool()
    with pytest.raises(RuntimeError):
        with connections("db") as conn:
            raise RuntimeError("boom")
    assert conn.rollbacks == 1
    conn.broken = True
    with connections("db") as again:
        pass
    assert again is conn  # handed out, then dropped on return
    with connections("db") as fresh:
        pass
    assert fresh is not conn and len(made) == 2


def test_idle_or_unfinished_sessions_are_never_handed_out():
    clock = Clock()
    connections, made = pool(clock)
    with connections("db") as conn:
        pass
    clock.now = 31
    with connections("db") as later:
        pass
    assert later is not conn and conn.closed  # idle too long
    later.info.transaction_status = pq.TransactionStatus.INTRANS
    with connections("db") as other:
        pass
    assert other is not later and later.closed


def test_one_idle_connection_per_database_and_close_ends_them():
    connections, made = pool()
    with connections("a") as a, connections("a") as b:  # two at once: two sessions
        pass
    assert a is not b and len(made) == 2
    assert sum(1 for c in made if c.closed) == 1  # only one kept idle
    with connections("b"):
        pass
    connections.close()
    assert all(c.closed for c in made)
