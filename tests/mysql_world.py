"""MySQL handoff world for the provider-neutral conformance kit.

Everything is observed independently of the adapter: content by its own row
digest and the counter from ``SHOW CREATE TABLE``, access from ``SHOW
GRANTS``, write attempts as the writer accounts themselves.
"""

import hashlib
import re
from uuid import uuid4

from mysql_business import PROBE_INSERT, SCHEMA, TABLES, denied

from sova.adapters.mysql_offline_copy import MySqlOfflineCopyAdapter


class Route:
    def __init__(self, database):
        self.database = database

    def get(self):
        return self.database

    def set(self, database):
        self.database = database


class MySqlWorld:
    def __init__(self, business):
        self.b, self.names = business, business["names"]
        self.writers = {"app": business["app"], "other": business["batch"]}
        self._route = Route(self.names["src"])
        self.pending = None

    def _db(self, side):
        return self.names["src" if side == "source" else "tgt"]

    def _adapter(self, source, target):
        return MySqlOfflineCopyAdapter(
            "mysql-offline-copy", connect=self.b["connect"], source_db=self._db(source),
            target_db=self._db(target), app_account=self.b["app"], tables=TABLES,
            schema_sql=SCHEMA, route=self._route, writer_accounts=tuple(self.writers.values()),
            session_wait_seconds=2.0)

    def adapter(self):
        return self._adapter("source", "target")

    def reverse_adapter(self):
        return self._adapter("target", "source")

    def _admin(self, query, args=None):
        with self.b["connect"](None) as conn, conn.cursor() as cur:
            cur.execute(query, args)  # no args: a '%' in an account name stays literal
            rows = cur.fetchall()
            conn.commit()
            return rows

    def can_write(self, side, writer):
        errors = self.b["pymysql"].err
        try:
            with self.b["connect_as"](self._db(side), self.writers[writer]) as conn:
                with conn.cursor() as cur:
                    cur.execute(PROBE_INSERT)
                conn.rollback()
            return True
        except (errors.OperationalError, errors.ProgrammingError, errors.InternalError) as e:
            if denied(e):
                return False
            raise

    def business_write(self, count):
        with self.b["connect_as"](self._db("source"), self.b["app"]) as conn, \
                conn.cursor() as cur:
            cur.execute("SELECT coalesce(max(id), 0) + 1 FROM orders")
            start = cur.fetchone()[0]
            for i in range(start, start + count):
                cur.execute("INSERT INTO orders VALUES (%s, %s, %s, %s)",
                            (i, f"customer-{i % 7}", f"key-{i}", i))
                cur.execute("INSERT INTO idempotency_results VALUES (%s, %s, 'confirmed')",
                            (f"customer-{i % 7}", f"key-{i}"))
            cur.execute("INSERT INTO order_numbers () VALUES ()")
            conn.commit()

    def business_delete(self, count):
        with self.b["connect_as"](self._db("source"), self.b["app"]) as conn, \
                conn.cursor() as cur:
            cur.execute("SELECT customer, idempotency_key FROM orders ORDER BY id LIMIT %s",
                        (count,))
            for customer, key in cur.fetchall():
                cur.execute("DELETE FROM idempotency_results WHERE customer=%s AND "
                            "idempotency_key=%s", (customer, key))
                cur.execute("DELETE FROM orders WHERE customer=%s AND idempotency_key=%s",
                            (customer, key))
            conn.commit()

    def content(self, side):
        db = self._db(side)
        if not self._admin("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                           (db,)):
            return None
        h = hashlib.sha256()
        with self.b["connect"](None) as conn, conn.cursor() as cur:
            cur.execute("SET SESSION time_zone='+00:00'")
            for table, _ in TABLES:  # a multiset of rows per table
                cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s "
                            "AND TABLE_NAME=%s", (db, table))
                if not cur.fetchone():
                    return None
                cur.execute(f"SELECT * FROM `{db}`.`{table}`")
                for text in sorted(repr(row) for row in cur.fetchall()):
                    h.update(f"{table}:{len(text)}:{text}".encode())
                cur.execute(f"SHOW CREATE TABLE `{db}`.`{table}`")
                counter = re.search(r"AUTO_INCREMENT=(\d+)", cur.fetchone()[1])
                h.update(f"{table}:counter:{counter[1] if counter else None}".encode())
            conn.commit()
        return h.hexdigest()

    def _access(self, side):
        db = self._db(side)
        out = []
        for writer in self.writers.values():
            grants = self._admin(f"SHOW GRANTS FOR {self.b['quoted'](writer)}")
            user, host = writer.rsplit("@", 1)
            locked = self._admin("SELECT account_locked FROM mysql.user WHERE user=%s AND "
                                 "host=%s", (user, host))[0][0]
            out.append((writer, locked, sorted(g[0] for g in grants if f"`{db}`" in g[0])))
        return out

    def state(self):
        prepared = [r[3].decode() if isinstance(r[3], bytes) else r[3]
                    for r in self._admin("XA RECOVER")]
        return (self.content("source"), self.content("target"), self._access("source"),
                self._access("target"), sorted(prepared), self._route.get())

    def reopen_source(self):
        """The source restored from a backup taken before the handoff (its
        grant tables too: the account is open and has its rights again)."""
        app = self.b["quoted"](self.b["app"])
        self._admin(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{self._db('source')}`.* TO {app}")
        self._admin(f"ALTER USER {app} ACCOUNT UNLOCK")

    def tamper_target(self):
        self._admin(f"UPDATE `{self._db('target')}`.orders SET amount = amount + 1 "
                    "WHERE id = 1")

    def move_route(self, side):
        self._route.set(self._db(side))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return self._db(side)

    def open_pending_write(self):
        """An XA transaction prepared by the application: it outlives the session."""
        self.pending = "sova-conformance-" + uuid4().hex[:8]
        conn = self.b["connect_as"](self._db("source"), self.b["app"])
        conn.autocommit(True)
        with conn.cursor() as cur:
            cur.execute(f"XA START '{self.pending}'")
            cur.execute("INSERT INTO orders VALUES (-7, 'pending', 'pending', 0)")
            cur.execute(f"XA END '{self.pending}'")
            cur.execute(f"XA PREPARE '{self.pending}'")
        conn.close()

    def resolve_pending_write(self):
        self._admin(f"XA ROLLBACK '{self.pending}'")
