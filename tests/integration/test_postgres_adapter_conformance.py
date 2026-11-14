"""The PostgreSQL offline-copy adapter against the provider-neutral conformance kit."""

import hashlib
from uuid import uuid4

from adapter_conformance import HandoffAdapterConformance
import psycopg
from psycopg import sql
from postgres_business import PROBE_INSERT, SCHEMA, TABLES, Route
import pytest

from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter

pytestmark = pytest.mark.postgres


class PostgresWorld:
    def __init__(self, business):
        self.b, self.names = business, business["names"]
        self.writers = {"app": business["app"], "other": business["batch"]}
        self._route = Route(self.names["src"])
        self.pending = None

    def _db(self, side):
        return self.names["src" if side == "source" else "tgt"]

    def _adapter(self, source, target):
        return PostgresOfflineCopyAdapter(
            "postgres-offline-copy", connect=self.b["connect"], source_db=self._db(source),
            target_db=self._db(target), app_role=self.b["app"], tables=TABLES,
            schema_sql=SCHEMA, route=self._route, writer_roles=tuple(self.writers.values()),
            session_wait_seconds=2.0)

    def adapter(self):
        return self._adapter("source", "target")

    def reverse_adapter(self):
        return self._adapter("target", "source")

    def can_write(self, side, writer):
        try:
            with self.b["connect_as"](self._db(side), self.writers[writer]) as conn:
                with conn.transaction(force_rollback=True):
                    conn.execute(PROBE_INSERT)
            return True
        except (psycopg.errors.InsufficientPrivilege, psycopg.OperationalError):
            return False

    def business_write(self, count):
        with self.b["connect_as"](self._db("source"), self.b["app"]) as conn:
            start = conn.execute("SELECT coalesce(max(id), 0) + 1 FROM orders").fetchone()[0]
            for i in range(start, start + count):
                conn.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                             (i, f"customer-{i % 7}", f"key-{i}", i))
                conn.execute("INSERT INTO idempotency_results VALUES (%s, %s, 'confirmed')",
                             (f"customer-{i % 7}", f"key-{i}"))
            conn.execute("SELECT nextval('order_number_seq')")

    def content(self, side):
        h = hashlib.sha256()
        with self.b["connect"](self._db(side)) as conn:
            for table, key in TABLES:
                for (text,) in conn.execute(sql.SQL("SELECT t::text FROM {} t ORDER BY {}")
                                            .format(sql.Identifier(table),
                                                    sql.Identifier(key))):
                    h.update(table.encode() + b":" + text.encode() + b"\n")
            for row in conn.execute("SELECT sequencename, last_value FROM pg_sequences "
                                    "WHERE schemaname='public' ORDER BY 1"):
                h.update(repr(row).encode())
        return h.hexdigest()

    def _access(self, side):
        db = self._db(side)
        with self.b["connect"](db) as conn:
            return sorted(
                (role, table, privilege, conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, table, privilege)).fetchone()[0])
                for role in self.writers.values() for table, _ in TABLES
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")) + sorted(
                (role, conn.execute("SELECT has_database_privilege(%s, %s, 'CONNECT')",
                                    (role, db)).fetchone()[0])
                for role in self.writers.values())

    def state(self):
        with self.b["connect"](self._db("source")) as conn:
            prepared = [r[0] for r in conn.execute("SELECT gid FROM pg_prepared_xacts")]
        return (self.content("source"), self.content("target"), self._access("source"),
                self._access("target"), sorted(prepared), self._route.get())

    def reopen_source(self):
        """The source restored from a backup taken before the handoff."""
        db, app = self._db("source"), sql.Identifier(self.b["app"])
        with self.b["connect"](db) as conn:
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(db), app))
            conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN "
                                 "SCHEMA public TO {}").format(app))

    def tamper_target(self):
        with self.b["connect"](self._db("target")) as conn:
            conn.execute("UPDATE orders SET amount = amount + 1 WHERE id = 1")

    def move_route(self, side):
        self._route.set(self._db(side))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return self._db(side)

    def open_pending_write(self):
        with self.b["connect"](self._db("source")) as conn:
            if conn.execute("SHOW max_prepared_transactions").fetchone()[0] == "0":
                pytest.skip("cluster started without prepared transactions")
        self.pending = "sova-conformance-" + uuid4().hex[:8]
        with self.b["connect_as"](self._db("source"), self.b["app"]) as conn:
            conn.autocommit = True
            conn.execute("BEGIN")
            conn.execute("INSERT INTO orders VALUES (-7, 'pending', 'pending', 0)")
            conn.execute(sql.SQL("PREPARE TRANSACTION {}").format(sql.Literal(self.pending)))

    def resolve_pending_write(self):
        with psycopg.connect(self.b["dsn"], dbname=self._db("source"),
                             autocommit=True) as admin:
            admin.execute(sql.SQL("ROLLBACK PREPARED {}").format(sql.Literal(self.pending)))


class TestPostgresOfflineCopyConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, business):
        return PostgresWorld(business)
