"""The platform's own administrative connections, reused instead of opened per call.

A handoff step opens a connection for every readback and probe. Across the
internet each one costs a TLS handshake and an authentication round trip:
measured on a cross-cloud handoff of 10k rows, 32 connections were opened
while writes were paused, 100-240 ms each, 4-7 s in all, about 40 % of the
pause. ``ReusedConnections`` wraps the platform's connection factory and keeps
at most one idle connection per database:

* each use is the same as a fresh connection: the transaction ends with the
  block (commit, or rollback on an error), and the session is reset with
  ``DISCARD ALL`` (settings, temporary tables, prepared statements, advisory
  locks, session authorization) before it is kept; kept sessions never
  auto-prepare statements, since the reset removes them under the driver;
* a connection idle longer than ``max_idle_seconds``, broken, or left in a
  transaction is never handed out again;
* ``close()`` ends every kept connection.

Only for the platform's administrative connections. A probe that tests
whether a role can open a session (write denial, single writer, cold start)
must keep opening fresh ones, or a revoked right would look intact. The
adapters' session checks count sessions of the registered writer roles only,
so a kept administrative session does not affect a fence.
"""

from contextlib import contextmanager
import threading
import time

from psycopg import pq

from sova.domain.contracts import ContractError


class ReusedConnections:
    def __init__(self, open_connection, *, max_idle_seconds=30.0, clock=time.monotonic):
        if not callable(open_connection):
            raise ContractError("A connection factory is required")
        if not 0 < max_idle_seconds <= 600:
            raise ContractError("Bounded idle time required")
        self.open_connection, self.clock = open_connection, clock
        self.max_idle_seconds = max_idle_seconds
        self._idle, self._lock = {}, threading.Lock()
        self.opened = 0

    def _take(self, database):
        with self._lock:
            kept = self._idle.pop(database, None)
        if kept is not None:
            conn, since = kept
            usable = (not conn.closed and conn.info.transaction_status
                      == pq.TransactionStatus.IDLE
                      and self.clock() - since <= self.max_idle_seconds)
            if usable:
                return conn
            conn.close()
        self.opened += 1
        conn = self.open_connection(database)
        # DISCARD ALL drops server-side prepared statements, but the driver's
        # cache would still name them ("prepared statement does not exist",
        # found on the first cloud run): kept sessions never auto-prepare.
        conn.prepare_threshold = None
        return conn

    def _keep(self, database, conn):
        try:
            if conn.closed or conn.broken:
                return
            conn.autocommit = True
            conn.execute("DISCARD ALL")
            conn.autocommit = False
        except Exception:  # noqa: BLE001 - a session that cannot be reset is dropped
            conn.close()
            return
        with self._lock:
            previous = self._idle.pop(database, None)
            self._idle[database] = (conn, self.clock())
        if previous is not None:
            previous[0].close()

    @contextmanager
    def _lease(self, database):
        conn = self._take(database)
        try:
            yield conn
        except BaseException:
            if not conn.closed:
                conn.rollback()
            self._keep(database, conn)
            raise
        if not conn.closed:
            conn.commit()
        self._keep(database, conn)

    def __call__(self, database):
        """A context manager giving a connection to ``database``, as ``connect`` did."""
        return self._lease(database)

    def close(self):
        with self._lock:
            kept, self._idle = list(self._idle.values()), {}
        for conn, _ in kept:
            conn.close()
