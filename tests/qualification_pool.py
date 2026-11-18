"""Test-only bounded standard pool; production connect() remains unchanged.

No business credentials/connections, tenant/trust cache or effect retry here.
Only context-manager callers can use connect(); never return a naked handle.
"""

from contextlib import contextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

from sova.domain.contracts import ContractError


class ControlPool:
    def __init__(self, dsn, role, *, max_size=4):
        if type(max_size) is not int or not 1 <= max_size <= 4 \
                or conninfo_to_dict(dsn).get('sslmode') != 'verify-full':
            raise ContractError('Bounded certificate-verified control pool required')
        self.role = role
        self.pool = ConnectionPool(dsn, kwargs={'user': role, 'connect_timeout': 10,
            'prepare_threshold': None}, min_size=0, max_size=max_size, max_waiting=4,
            timeout=1, reconnect_timeout=1, num_workers=1, max_lifetime=60, max_idle=10,
            open=False, name='qualification-control', check=self.check, reset=self.reset)

    def check(self, conn):
        # CONNECT/NOLOGIN changes do not end existing sessions automatically.
        # Recheck current authorization before lending a previously opened session.
        with conn.transaction():
            valid = conn.execute("SELECT session_user=current_user AND session_user=%s "
                "AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls "
                "AND has_database_privilege(session_user,current_database(),'CONNECT') "
                "AND coalesce((SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()),false) "
                "FROM pg_roles WHERE rolname=session_user", (self.role,)).fetchone()
        if valid != (True,):
            raise psycopg.OperationalError('Control pool session authorization rejected')

    @staticmethod
    def reset(conn):
        # Pool supplies an idle connection after normal commit/exception rollback.
        # DISCARD ALL also releases temp objects, prepared statements and locks.
        conn.autocommit = True
        conn.execute('DISCARD ALL')
        conn.isolation_level = None
        conn.read_only = None
        conn.deferrable = None
        conn.row_factory = tuple_row
        conn.prepare_threshold = None
        conn.autocommit = False

    def __enter__(self):
        self.pool.open()
        return self

    def __exit__(self, *exc):
        self.pool.close()

    @contextmanager
    def connect(self):
        with self.pool.connection(timeout=1) as conn:
            yield conn
