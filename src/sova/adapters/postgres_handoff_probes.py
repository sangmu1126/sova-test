"""Independent probes for a PostgreSQL handoff (§16.3, §15.5, §28.2).

Probes observe the databases separately from the adapter that changed them.
Write denial needs two views to agree (ADR-0007): an attempt - the role
connects and makes a real write inside a transaction that is always rolled
back - and the server's own account of the role, as the administrator reads
it: whether it may change a unit table, and whether it can get a session to
do it (log in and connect, or one already open). A refused statement, or a
refused connection on a server the administrator reaches, that the server's
view agrees with is a denial. A connection that fails on a server the
administrator cannot reach decides nothing (psycopg gives no SQLSTATE for a
refused connection, so reachability tells a refusal from a network fault).
Content probes compare row sets by an ordered SHA-256 over each row's text
form. Each probe returns ``(passed, observation)``; the
observation is what a signed evidence record's artifact digest is taken over.

These probes measure a local reference profile. Transport encryption is
reported as observed (``ssl`` on the server), not assumed.
"""

import hashlib

import psycopg
from psycopg import sql

from sova.adapters.postgres_digest import table_digest
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

    def _reachable(self, database):
        try:
            with self.admin_connect(database) as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.OperationalError:
            return False

    def _try_write(self, which, role=None):
        """True when a real write by ``role`` (default: the application) would commit."""
        database = self._db(which)
        try:
            with self.connect_as(database, role or self.app_role) as conn:
                with conn.transaction(force_rollback=True):
                    conn.execute(self.probe_insert)
            return True, "write_accepted"
        except psycopg.errors.InsufficientPrivilege:
            return False, "write_privilege_denied"
        except psycopg.OperationalError:
            if self._reachable(database):
                return False, "connection_refused"
            return False, "undecided:unreachable"

    def _may_write(self, which, role):
        """The server's view of ``role``: unit tables it may change, and whether
        it can get a session (log in and connect, or one already open)."""
        with self.admin_connect(self._db(which)) as conn:
            found = conn.execute(
                "SELECT r.rolcanlogin, has_database_privilege(r.oid, current_database(), "
                "'CONNECT'), (SELECT count(*) FROM pg_stat_activity a WHERE "
                "a.usename = r.rolname AND a.datname = current_database()) "
                "FROM pg_roles r WHERE r.rolname = %s", (role,)).fetchone()
            if found is None:
                return {"may_write": False, "role_exists": False}
            can_login, can_connect, sessions = found
            writable = [table for table, _ in self.tables if conn.execute(
                "SELECT coalesce(has_table_privilege(%s, to_regclass(%s), "
                "'INSERT,UPDATE,DELETE,TRUNCATE'), false)",
                (role, sql.Identifier(table).as_string(conn))).fetchone()[0]]
        session = bool(can_login and can_connect) or sessions > 0
        return {"may_write": bool(writable) and session, "writable_tables": writable,
                "can_log_in": can_login, "can_connect": can_connect, "open_sessions": sessions}

    def _denied(self, which, role):
        wrote, attempt = self._try_write(which, role)
        if attempt.startswith("undecided"):  # nor can the administrator ask the server
            return False, {"attempt": attempt}
        view = self._may_write(which, role)
        denied = not wrote and attempt in ("write_privilege_denied", "connection_refused") \
            and not view["may_write"]
        return denied, {"attempt": attempt, **view}

    def write_denied(self, which):
        outcomes = {role: self._denied(which, role) for role in self.writer_roles}
        return all(d for d, _ in outcomes.values()), {
            "probe": "write_denied", "database": self._db(which),
            "outcomes": {role: detail for role, (_, detail) in outcomes.items()}}

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

    def _rows(self, which, table, *, database=None):
        """Content as a multiset: row texts sorted bytewise and length-framed, so
        neither physical order nor a non-unique key can make equal content
        differ; computed on the server where the table is small enough."""
        with self.admin_connect(database or self._db(which)) as conn:
            return table_digest(conn, table)

    def content_equal(self, table):
        if table not in dict(self.tables):
            raise ContractError("Unregistered table")
        source, target = self._rows("source", table), self._rows("target", table)
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
        """This session's own channel, not the server's setting: the server
        reports the backend's TLS (``pg_stat_ssl``), and this client must
        have verified the certificate chain and host name (``verify-full``).
        A server that merely has TLS enabled says nothing about a connection
        that fell back to plaintext or skipped verification."""
        with self.admin_connect(self._db(which)) as conn:
            row = conn.execute("SELECT ssl, version FROM pg_stat_ssl "
                               "WHERE pid = pg_backend_pid()").fetchone()
            mode = conn.info.get_parameters().get("sslmode") or "default"
        encrypted = bool(row and row[0])
        verified = encrypted and mode == "verify-full"
        return verified, {"probe": "transport_encrypted", "encrypted": encrypted,
                          "tls_version": row[1] if encrypted else None,
                          "client_verification": mode}

    def cross_tenant_denied(self, other_db):
        """The application role cannot connect to another tenant's database: its
        connection is refused by a server the administrator reaches, and that
        server does not grant it ``CONNECT`` there."""
        try:
            with self.connect_as(other_db, self.app_role) as conn:
                conn.execute("SELECT 1")
            return False, {"probe": "cross_tenant_denied", "outcome": "connected"}
        except psycopg.OperationalError:
            pass
        try:
            with self.admin_connect(other_db) as conn:
                granted = conn.execute("SELECT has_database_privilege(%s, current_database(), "
                                       "'CONNECT')", (self.app_role,)).fetchone()[0]
        except psycopg.OperationalError:
            return False, {"probe": "cross_tenant_denied", "outcome": "undecided:unreachable"}
        return not granted, {"probe": "cross_tenant_denied", "outcome": "connection_refused",
                             "connect_granted": granted}

    def restore_drill(self, scratch_db):
        """Restore and verify one consistent read-only source snapshot.

        Comparing the restored copy to later live reads would misclassify a
        concurrent legitimate commit, and READ COMMITTED can mix table epochs.
        The scratch write transaction also keeps verification isolated from
        another drill's subsequent TRUNCATE. This is not a durable backup/PITR.
        """
        if scratch_db in (self.source_db, self.target_db):
            raise ContractError("Restore scratch must be a separate database")
        with self.admin_connect(self.target_db) as source, \
                self.admin_connect(scratch_db) as scratch:
            source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            snapshot, isolation, read_only = source.execute(
                "SELECT pg_current_snapshot()::text,current_setting('transaction_isolation'),"
                "current_setting('transaction_read_only')").fetchone()
            if isolation != "repeatable read" or read_only != "on":
                raise ContractError("Restore source snapshot configuration invalid")
            with scratch.transaction():
                expected = {}
                for table, key in self.tables:
                    expected[table] = table_digest(source, table)
                    scratch.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(table)))
                    with source.cursor().copy(sql.SQL(
                        "COPY (SELECT * FROM {} ORDER BY {}) TO STDOUT").format(
                            sql.Identifier(table), sql.Identifier(key))) as out, \
                            scratch.cursor().copy(sql.SQL("COPY {} FROM STDIN").format(
                                sql.Identifier(table))) as into:
                        for chunk in out:
                            into.write(chunk)
                restored = {table: expected[table] == table_digest(scratch, table) for table, _ in self.tables}
        return all(restored.values()), {"probe": "restore_drill", "tables": restored,
            "source_snapshot": snapshot, "source_isolation": isolation, "source_read_only": True,
            "comparison": "copied_snapshot", "durable_backup_qualified": False}
