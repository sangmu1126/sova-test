"""Independent probes for a PostgreSQL handoff (§16.3, §15.5, §28.2).

Probes observe the databases separately from the adapter that changed them.
Write probes connect as the application role and attempt a real write inside
a transaction that is always rolled back; a refused connection or a refused
statement is a denial. Content probes compare row sets by an ordered SHA-256
over each row's text form. Each probe returns ``(passed, observation)``; the
observation is what a signed evidence record's artifact digest is taken over.

These probes measure a local reference profile. Transport encryption is
reported as observed (``ssl`` on the server), not assumed.
"""

import hashlib

import psycopg
from psycopg import sql

from sova.domain.contracts import ContractError, canonical_json


def artifact_digest(observation):
    return hashlib.sha256(canonical_json(observation, limit=65536).encode()).hexdigest()


class PostgresHandoffProbes:
    def __init__(self, *, connect_as, admin_connect, source_db, target_db, app_role, tables,
                 probe_insert, writer_roles=None):
        if not callable(connect_as) or not callable(admin_connect):
            raise ContractError("Application-role and administrative connections required")
        self.connect_as, self.admin_connect = connect_as, admin_connect
        self.source_db, self.target_db, self.app_role = source_db, target_db, app_role
        self.tables, self.probe_insert = tables, probe_insert
        # Every registered writer path is probed, not only the application.
        self.writer_roles = tuple(writer_roles) if writer_roles else (app_role,)

    def _db(self, which):
        return self.source_db if which == "source" else self.target_db

    def _try_write(self, which, role=None):
        """True when a real write by ``role`` (default: the application) would commit."""
        try:
            with self.connect_as(self._db(which), role or self.app_role) as conn:
                with conn.transaction(force_rollback=True):
                    conn.execute(self.probe_insert)
            return True, "write_accepted"
        except psycopg.errors.InsufficientPrivilege:
            return False, "write_privilege_denied"
        except psycopg.OperationalError:
            return False, "connection_refused"

    def write_denied(self, which):
        outcomes = {role: self._try_write(which, role) for role in self.writer_roles}
        return not any(wrote for wrote, _ in outcomes.values()), {
            "probe": "write_denied", "database": self._db(which),
            "outcomes": {role: outcome for role, (_, outcome) in outcomes.items()}}

    def cold_start_without_source(self):
        """A fresh application session completes a write on the target while the
        source cannot be reached at all (independence drill, §20.1, AT-35)."""
        try:
            with self.connect_as(self.source_db, self.app_role):
                pass
            source = "reachable"
        except psycopg.OperationalError:
            source = "unreachable"
        wrote, outcome = self._try_write("target")
        return source == "unreachable" and wrote, {
            "probe": "cold_start_without_source", "source": source, "target": outcome}

    def single_writer(self):
        target, target_outcome = self._try_write("target")
        source_denied, source = self.write_denied("source")
        return target and source_denied, {"probe": "single_writer",
                                          "target": target_outcome,
                                          "source": source["outcomes"]}

    def _rows(self, which, table, key):
        with self.admin_connect(self._db(which)) as conn:
            rows = conn.execute(sql.SQL("SELECT t::text FROM {} t ORDER BY {}").format(
                sql.Identifier(table), sql.Identifier(key))).fetchall()
        h = hashlib.sha256()
        for (text,) in rows:
            h.update(text.encode() + b"\n")
        return {"rows": len(rows), "sha256": h.hexdigest()}

    def content_equal(self, table):
        key = dict(self.tables)[table]
        source, target = self._rows("source", table, key), self._rows("target", table, key)
        return source == target, {"probe": "content_equal", "table": table,
                                  "source": source, "target": target}

    def order_effects_once(self):
        """ORDER-01: one confirmed order per customer and idempotency key, each
        with its recorded result, on the database that now holds authority."""
        with self.admin_connect(self.target_db) as conn:
            duplicates = conn.execute(
                "SELECT count(*) FROM (SELECT customer, idempotency_key FROM orders "
                "GROUP BY 1, 2 HAVING count(*) > 1) d").fetchone()[0]
            unrecorded = conn.execute(
                "SELECT count(*) FROM orders o LEFT JOIN idempotency_results r "
                "USING (customer, idempotency_key) WHERE r.customer IS NULL").fetchone()[0]
        return duplicates == 0 and unrecorded == 0, {
            "probe": "order_effects_once", "duplicates": duplicates, "unrecorded": unrecorded}

    def zone(self, which, allowed):
        with self.admin_connect(self._db(which)) as conn:
            label = conn.execute(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname=%s", (self._db(which),)).fetchone()[0]
        return label in allowed, {"probe": "zone", "database": self._db(which),
                                  "label": label, "allowed": sorted(allowed)}

    def transport_encrypted(self, which):
        with self.admin_connect(self._db(which)) as conn:
            ssl = conn.execute("SHOW ssl").fetchone()[0]
        return ssl == "on", {"probe": "transport_encrypted", "ssl": ssl}

    def cross_tenant_denied(self, other_db):
        try:
            with self.connect_as(other_db, self.app_role) as conn:
                conn.execute("SELECT 1")
            return False, {"probe": "cross_tenant_denied", "outcome": "connected"}
        except psycopg.OperationalError:
            return True, {"probe": "cross_tenant_denied", "outcome": "connection_refused"}

    def restore_drill(self, scratch_db):
        """Copy the authoritative tables into an empty database and compare."""
        with self.admin_connect(self.target_db) as source, \
                self.admin_connect(scratch_db) as scratch:
            with scratch.transaction():
                for table, key in self.tables:
                    scratch.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(table)))
                    with source.cursor().copy(sql.SQL(
                        "COPY (SELECT * FROM {} ORDER BY {}) TO STDOUT").format(
                            sql.Identifier(table), sql.Identifier(key))) as out, \
                            scratch.cursor().copy(sql.SQL("COPY {} FROM STDIN").format(
                                sql.Identifier(table))) as into:
                        for chunk in out:
                            into.write(chunk)
        restored = {}
        for table, key in self.tables:
            original = self._rows("target", table, key)
            with self.admin_connect(scratch_db) as conn:
                rows = conn.execute(sql.SQL("SELECT t::text FROM {} t ORDER BY {}").format(
                    sql.Identifier(table), sql.Identifier(key))).fetchall()
            h = hashlib.sha256()
            for (text,) in rows:
                h.update(text.encode() + b"\n")
            restored[table] = original == {"rows": len(rows), "sha256": h.hexdigest()}
        return all(restored.values()), {"probe": "restore_drill", "tables": restored}
