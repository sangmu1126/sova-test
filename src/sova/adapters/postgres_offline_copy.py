"""Reference adapter: PostgreSQL-to-PostgreSQL offline copy of one unit (§16.1, §16.5).

It performs the handoff's external effects on two reachable databases and
reads each postcondition back from the databases themselves, never from its
own memory:

* stage_target - the target tables exist and the application role cannot
  connect to or write the target;
* verify_return_target (failback) - the original source still has its tables,
  stays fenced and has no application session;
* start_sync - an initial copy taken while the source still accepts writes,
  marked per table on the target;
* pause_admission - the application role can no longer connect to the source;
* fence_source_writes - no registered writer role (the application, batch
  jobs, ...) holds a write privilege on any source table or has a live session
  there, and no prepared transaction is left that could still commit (§15.5);
* copy_content / apply_final_changes - every table, and the state of every
  sequence (so identifiers are never issued twice, AT-24), is copied inside
  one target transaction; this reference adapter applies the final changes by
  a full re-copy inside the fenced window rather than by change data capture;
* verify_final_state - per-table row count and content digest, and every
  sequence's position, are equal;
* activate_target - the role can connect to and write the target while the
  source stays fenced (single writer);
* commit_route - the route points at the target.

Every action is idempotent. A postcondition that does not hold is reported as
UNKNOWN: current state alone never proves that an action had no effect
(§14.3). The adapter needs an administrative connection to both databases;
it is a reference for one PostgreSQL profile, not a general data mover.
"""

import hashlib
import time

from psycopg import sql

from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
_MARKS = "sova_handoff_sync_marks"
_WRITE = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


class PostgresOfflineCopyAdapter:
    def __init__(self, adapter_id, *, connect, source_db, target_db, app_role, tables,
                 schema_sql, route, writer_roles=None, session_wait_seconds=5.0):
        if not callable(connect) or not all(
            callable(getattr(route, n, None)) for n in ("get", "set")
        ):
            raise ContractError("Administrative connect and a route store required")
        if type(tables) is not tuple or not tables:
            raise ContractError("Tables with their key columns required")
        self.adapter_id = adapter_id
        self.connect, self.route = connect, route
        self.source_db, self.target_db, self.app_role = source_db, target_db, app_role
        # Every registered writer path is fenced; only the application role is
        # activated on the target.
        self.writer_roles = tuple(writer_roles) if writer_roles else (app_role,)
        if app_role not in self.writer_roles:
            raise ContractError("The application role must be a registered writer")
        self.tables, self.schema_sql = tables, schema_sql
        self.session_wait_seconds = session_wait_seconds

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported handoff action for this adapter")

    def apply(self, command):
        getattr(self, "_" + command.action)()

    def readback(self, command, now):
        holds, detail = getattr(self, "_check_" + command.action)()
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **detail}, limit=65536))

    # -- helpers ---------------------------------------------------------------
    def _db(self, which):
        return self.source_db if which == "source" else self.target_db

    def _execute(self, which, statements):
        with self.connect(self._db(which)) as conn:
            for statement in statements:
                conn.execute(statement)

    def _role(self):
        return sql.Identifier(self.app_role)

    def _writers(self):
        return sql.SQL(", ").join(sql.Identifier(r) for r in self.writer_roles)

    def _can_write(self, which, roles=None):
        roles = roles or self.writer_roles
        with self.connect(self._db(which)) as conn:
            return {
                f"{role}:{table}": any(conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, table, privilege)).fetchone()[0] for privilege in _WRITE)
                for role in roles for table, _ in self.tables
            }

    def _can_connect(self, which, roles=None):
        roles = roles or self.writer_roles
        with self.connect(self._db(which)) as conn:
            return {role: conn.execute("SELECT has_database_privilege(%s, %s, 'CONNECT')",
                                       (role, self._db(which))).fetchone()[0]
                    for role in roles}

    def _sessions(self, which):
        with self.connect(self._db(which)) as conn:
            return conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = ANY(%s) "
                "AND datname=%s", (list(self.writer_roles), self._db(which))).fetchone()[0]

    def _prepared(self, which):
        with self.connect(self._db(which)) as conn:
            return [gid for (gid,) in conn.execute(
                "SELECT gid FROM pg_prepared_xacts WHERE database=%s ORDER BY gid",
                (self._db(which),))]

    def _digests(self, which):
        out = {}
        with self.connect(self._db(which)) as conn:
            for table, key in self.tables:
                rows = conn.execute(sql.SQL("SELECT t::text FROM {} t ORDER BY {}").format(
                    sql.Identifier(table), sql.Identifier(key))).fetchall()
                h = hashlib.sha256()
                for (text,) in rows:
                    h.update(text.encode() + b"\n")
                out[table] = {"rows": len(rows), "sha256": h.hexdigest()}
        return out

    def _sequences(self, which):
        with self.connect(self._db(which)) as conn:
            return {name: [last, called] for name, last, called in conn.execute(
                "SELECT sequencename, last_value, last_value IS NOT NULL FROM pg_sequences "
                "WHERE schemaname='public' ORDER BY sequencename")}

    def _no_access(self, which, *, connect_too):
        statements = [sql.SQL("REVOKE {} ON ALL TABLES IN SCHEMA public FROM PUBLIC, {}").format(
            sql.SQL(", ").join(sql.SQL(p) for p in _WRITE), self._writers()),
            # nextval() changes state too: a fenced writer must not draw numbers.
            sql.SQL("REVOKE USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA public "
                    "FROM PUBLIC, {}").format(self._writers())]
        if connect_too:
            statements.append(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC, {}").format(
                sql.Identifier(self._db(which)), self._writers()))
        self._execute(which, statements)

    # -- actions ---------------------------------------------------------------
    def _stage_target(self):
        self._execute("target", [self.schema_sql])
        self._no_access("target", connect_too=True)

    def _check_stage_target(self):
        with self.connect(self.target_db) as conn:
            present = all(conn.execute("SELECT to_regclass(%s) IS NOT NULL",
                                       (table,)).fetchone()[0] for table, _ in self.tables)
        writable = self._can_write("target")
        connect = self._can_connect("target")
        return present and not any(writable.values()) and not any(connect.values()), {
            "tables_present": present, "target_writable": writable,
            "target_connectable": connect}

    def _pause_admission(self):
        self._execute("source", [sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC, {}").format(
            sql.Identifier(self.source_db), self._writers())])

    def _check_pause_admission(self):
        connect = self._can_connect("source")
        return not any(connect.values()), {"source_connectable": connect}

    def _fence_source_writes(self):
        self._no_access("source", connect_too=True)
        with self.connect(self.source_db) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = ANY(%s) AND datname=%s AND pid <> pg_backend_pid()",
                (list(self.writer_roles), self.source_db))
        deadline = time.monotonic() + self.session_wait_seconds
        while self._sessions("source") and time.monotonic() < deadline:
            time.sleep(0.05)

    def _check_fence_source_writes(self):
        writable = self._can_write("source")
        sessions = self._sessions("source")
        # A prepared transaction survives its session and can still commit.
        prepared = self._prepared("source")
        return not any(writable.values()) and sessions == 0 and not prepared, {
            "source_writable": writable, "source_sessions": sessions,
            "prepared_transactions": prepared}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        present, detail = self._check_stage_target()
        sessions = self._sessions("target")
        return present and sessions == 0, {**detail, "target_sessions": sessions}

    def _start_sync(self):
        self._copy_content()
        with self.connect(self.target_db) as conn:
            conn.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} (table_name text PRIMARY KEY, "
                "copied_at timestamptz NOT NULL)").format(sql.Identifier(_MARKS)))
            conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(sql.Identifier(_MARKS)))
            for table, _ in self.tables:
                conn.execute(sql.SQL(
                    "INSERT INTO {} VALUES (%s, clock_timestamp()) ON CONFLICT (table_name) "
                    "DO UPDATE SET copied_at=EXCLUDED.copied_at").format(sql.Identifier(_MARKS)),
                    (table,))

    def _check_start_sync(self):
        with self.connect(self.target_db) as conn:
            exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (_MARKS,)).fetchone()[0]
            marked = set() if not exists else {r[0] for r in conn.execute(
                sql.SQL("SELECT table_name FROM {}").format(sql.Identifier(_MARKS)))}
        tables = {t for t, _ in self.tables}
        return tables <= marked, {"synced_tables": sorted(marked & tables)}

    def _apply_final_changes(self):
        self._copy_content()

    def _check_apply_final_changes(self):
        return self._check_copy_content()

    def _copy_content(self):
        with self.connect(self.source_db) as source, self.connect(self.target_db) as target:
            with target.transaction():
                for table, key in self.tables:
                    target.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(table)))
                    with source.cursor().copy(sql.SQL(
                        "COPY (SELECT * FROM {} ORDER BY {}) TO STDOUT").format(
                            sql.Identifier(table), sql.Identifier(key))) as out, \
                            target.cursor().copy(sql.SQL("COPY {} FROM STDIN").format(
                                sql.Identifier(table))) as into:
                        for chunk in out:
                            into.write(chunk)
                for name, (last, called) in self._sequences("source").items():
                    target.execute(sql.SQL(
                        "SELECT setval({}, coalesce(%s, (SELECT start_value FROM pg_sequences "
                        "WHERE schemaname='public' AND sequencename=%s)), %s)").format(
                            sql.Literal(name)), (last, name, called))

    def _check_copy_content(self):
        source, target = self._digests("source"), self._digests("target")
        rows = {t: [source[t]["rows"], target[t]["rows"]] for t in source}
        return all(a == b for a, b in rows.values()), {"rows": rows}

    def _verify_final_state(self):
        return None  # measurement only; the readback is the comparison

    def _check_verify_final_state(self):
        source, target = self._digests("source"), self._digests("target")
        sequences = self._sequences("source"), self._sequences("target")
        return source == target and sequences[0] == sequences[1], {
            "source": source, "target": target,
            "sequences": {"source": sequences[0], "target": sequences[1]}}

    def _activate_target(self):
        self._execute("target", [
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(self.target_db), self._role()),
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    "TO {}").format(self._role()),
            sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public "
                    "TO {}").format(self._role()),
        ])

    def _check_activate_target(self):
        target = self._can_write("target", (self.app_role,))
        source = self._can_write("source")
        connect = self._can_connect("target", (self.app_role,))[self.app_role]
        return connect and all(target.values()) and not any(source.values()), {
            "target_writable": target, "source_writable": source,
            "target_connectable": connect}

    def _commit_route(self):
        self.route.set(self.target_db)

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.target_db, {"route": current}
