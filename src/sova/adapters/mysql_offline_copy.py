"""Reference adapter: MySQL-to-MySQL offline copy of one unit (§16.1, §16.5).

The same actions and postconditions as the PostgreSQL reference adapter, read
back from the servers themselves. What differs is how MySQL grants and
sessions work, and each difference makes the fence stricter, never looser:

* There is no per-database CONNECT right, and "no privilege on it" would
  already be the fence. Pausing admission is the account lock: no new session
  of a writer account on the source server, while its grants (still to be
  fenced) and open sessions remain. The application account is unlocked again
  at activation, on the target server; where that is the source server too,
  the source stays fenced by having no grant there.
* A session caches the database-level privileges of its current database
  until its next ``USE``, and a session is not bound to one database: a
  transaction that wrote a source table may sit in a session now using
  another database and still commit. So the fence ends *every* session of a
  registered writer account, not only those using the source database.
* A prepared XA transaction survives its session and can still commit.
  ``XA RECOVER`` does not say which database it touched, so any prepared XA
  transaction on the source server keeps the fence unconfirmed until an
  operator resolves it.
* Privileges also come through roles (``mysql.role_edges``, transitively) and
  global grants. The fence reads them all; it revokes only the writer
  accounts' own database- and table-level grants. Access a role or a global
  grant still gives is reported and keeps the fence unconfirmed: removing a
  shared role is the operator's decision.
* ``AUTO_INCREMENT`` counters play the part of sequences: they are copied, so
  identifiers are never issued twice (AT-24), and compared at verification.
* ``TRUNCATE`` and ``ALTER`` commit implicitly. The copy deletes and inserts
  inside one target transaction; counters are set after it commits, and the
  readback checks both, so a copy cut between the two reads back UNKNOWN.

Writer accounts are given as ``user@host`` (the account's host pattern). The
content digest is the shared row digest over a canonical text of each row,
with the session time zone pinned to UTC; generated columns are compared but
never written. Every action is idempotent; a postcondition that does not
hold reads back UNKNOWN (§14.3).
"""

from contextlib import contextmanager
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal
import json
import re
import time

from sova.adapters.row_digest import client_digest
from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
MARKS = "sova_handoff_sync_marks"
# Privileges that change a database's content or what runs in it.
WRITE_PRIVILEGES = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "INDEX", "TRIGGER", "EVENT",
    "EXECUTE", "CREATE ROUTINE", "ALTER ROUTINE", "CREATE VIEW", "REFERENCES",
    "CREATE TEMPORARY TABLES", "LOCK TABLES", "ALL PRIVILEGES"})
_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")
_USER = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_HOST = re.compile(r"[A-Za-z0-9_.%:-]{1,255}")
_AUTO = re.compile(r" AUTO_INCREMENT=\d+")
_BATCH = 1000


def name(value):
    if type(value) is not str or not _NAME.fullmatch(value):
        raise ContractError("Plain MySQL identifier required")
    return f"`{value}`"


def account(value):
    """``user@host`` -> (user, host), validated."""
    if type(value) is not str or "@" not in value:
        raise ContractError("Writer account as user@host required")
    user, host = value.rsplit("@", 1)
    if not _USER.fullmatch(user) or not _HOST.fullmatch(host):
        raise ContractError("Plain MySQL account name required")
    return user, host


def quoted(value):
    user, host = account(value)
    return f"'{user}'@'{host}'"


def row_text(row):
    """Canonical text of one row: the same on any two servers for equal data."""
    def one(v):
        if v is None or isinstance(v, (bool, int, str)):
            return v
        if isinstance(v, float):
            return {"f": repr(v)}
        if isinstance(v, Decimal):
            return {"d": str(v)}
        if isinstance(v, (bytes, bytearray)):
            return {"b": bytes(v).hex()}
        if isinstance(v, (datetime, date, clock_time)):
            return {"t": v.isoformat()}
        if isinstance(v, timedelta):
            return {"i": v.total_seconds()}
        if isinstance(v, (set, frozenset)):
            return {"s": sorted(v)}
        raise ContractError(f"No canonical text for {type(v).__name__}")
    return json.dumps([one(v) for v in row], ensure_ascii=False, separators=(",", ":"))


class MySqlOfflineCopyAdapter:
    def __init__(self, adapter_id, *, connect, source_db, target_db, app_account, tables,
                 schema_sql, route, writer_accounts=None, session_wait_seconds=5.0):
        """``connect(database)`` opens an administrative PyMySQL connection to
        the server that holds ``database``, with no default database selected:
        every statement here names its database."""
        if not callable(connect) or not all(
                callable(getattr(route, n, None)) for n in ("get", "set")):
            raise ContractError("Administrative connect and a route store required")
        if type(tables) is not tuple or not tables:
            raise ContractError("Tables with their key columns required")
        for table, key in tables:
            name(table), name(key)
        name(source_db), name(target_db)
        self.adapter_id, self.connect, self.route = adapter_id, connect, route
        self.source_db, self.target_db = source_db, target_db
        self.app_account = app_account
        self.writer_accounts = tuple(writer_accounts) if writer_accounts else (app_account,)
        if app_account not in self.writer_accounts:
            raise ContractError("The application account must be a registered writer")
        for writer in self.writer_accounts:
            account(writer)
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

    # -- sessions --------------------------------------------------------------
    def _db(self, which):
        return self.source_db if which == "source" else self.target_db

    @contextmanager
    def _session(self, which):
        conn = self.connect(self._db(which))
        try:
            conn.autocommit(False)
            with conn.cursor() as cur:
                cur.execute("SET SESSION time_zone='+00:00'")
                cur.execute("SET SESSION information_schema_stats_expiry=0")
                yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _rows(self, which, query, args=None):
        with self._session(which) as cur:
            cur.execute(query, args or None)  # without args, '%' stays literal
            return cur.fetchall()

    def _exists(self, which, table):
        return bool(self._rows(which, "SELECT 1 FROM information_schema.TABLES WHERE "
                                      "TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                               (self._db(which), table)))

    def _database_exists(self, which):
        return bool(self._rows(which, "SELECT 1 FROM information_schema.SCHEMATA WHERE "
                                      "SCHEMA_NAME=%s", (self._db(which),)))

    # -- privileges ------------------------------------------------------------
    def _holders(self, which, writers):
        """Each writer with the roles it may activate, transitively."""
        rows = self._rows(which, "SELECT FROM_USER, FROM_HOST, TO_USER, TO_HOST "
                                 "FROM mysql.role_edges")
        edges = {}
        for from_user, from_host, to_user, to_host in rows:
            edges.setdefault(f"{to_user}@{to_host}", set()).add(f"{from_user}@{from_host}")
        out = {}
        for writer in writers:
            seen, todo = set(), [writer]
            while todo:
                current = todo.pop()
                if current not in seen:
                    seen.add(current)
                    todo.extend(edges.get(current, ()))
            out[writer] = sorted(seen)
        return out

    def _access(self, which, writers=None):
        """writer -> the privileges it holds on the database, by where they come from."""
        db = self._db(which)
        holders = self._holders(which, writers or self.writer_accounts)
        grantees = sorted({quoted(h) for hs in holders.values() for h in hs})
        found = {g: set() for g in grantees}
        if grantees:
            marks = ", ".join(["%s"] * len(grantees))
            for grantee, privilege in self._rows(
                    which, f"SELECT GRANTEE, PRIVILEGE_TYPE FROM information_schema."
                    f"SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA=%s AND GRANTEE IN ({marks})",
                    (db, *grantees)):
                found[grantee].add(f"database:{privilege}")
            for grantee, table, privilege in self._rows(
                    which, f"SELECT GRANTEE, TABLE_NAME, PRIVILEGE_TYPE FROM information_schema."
                    f"TABLE_PRIVILEGES WHERE TABLE_SCHEMA=%s AND GRANTEE IN ({marks})",
                    (db, *grantees)):
                found[grantee].add(f"table:{table}:{privilege}")
            for grantee, privilege in self._rows(
                    which, f"SELECT GRANTEE, PRIVILEGE_TYPE FROM information_schema."
                    f"USER_PRIVILEGES WHERE GRANTEE IN ({marks})", tuple(grantees)):
                if privilege in WRITE_PRIVILEGES or privilege == "SELECT":
                    found[grantee].add(f"global:{privilege}")
        return {writer: sorted(f"{h}:{p}" if h != writer else p
                               for h in hs for p in found[quoted(h)])
                for writer, hs in holders.items()}

    @staticmethod
    def _writes(privileges):
        return [p for p in privileges if p.rsplit(":", 1)[-1] in WRITE_PRIVILEGES]

    def _sessions(self):
        """Live sessions of the writer accounts' users, on the source server."""
        users = sorted({account(w)[0] for w in self.writer_accounts})
        marks = ", ".join(["%s"] * len(users))
        return [list(r) for r in self._rows(
            "source", f"SELECT ID, USER, DB FROM information_schema.PROCESSLIST "
            f"WHERE USER IN ({marks}) AND ID <> CONNECTION_ID() ORDER BY ID", tuple(users),)]

    def _sessions_on(self, which):
        users = sorted({account(w)[0] for w in self.writer_accounts})
        marks = ", ".join(["%s"] * len(users))
        return len(self._rows(
            which, f"SELECT ID FROM information_schema.PROCESSLIST WHERE USER IN ({marks}) "
            "AND DB=%s AND ID <> CONNECTION_ID()", (*users, self._db(which)),))

    def _prepared_xa(self):
        return [r[3].decode() if isinstance(r[3], bytes) else r[3]
                for r in self._rows("source", "XA RECOVER")]

    def _revoke_all(self, which, writers):
        db = self._db(which)
        with self._session(which) as cur:
            for writer in writers:
                cur.execute(f"REVOKE IF EXISTS ALL PRIVILEGES ON {name(db)}.* "
                            f"FROM {quoted(writer)} IGNORE UNKNOWN USER")
                user, host = account(writer)
                cur.execute("SELECT DISTINCT TABLE_NAME FROM information_schema."
                            "TABLE_PRIVILEGES WHERE TABLE_SCHEMA=%s AND GRANTEE=%s",
                            (db, quoted(writer)))
                for (table,) in cur.fetchall():
                    cur.execute(f"REVOKE IF EXISTS ALL PRIVILEGES ON {name(db)}.{name(table)} "
                                f"FROM {quoted(writer)} IGNORE UNKNOWN USER")

    # -- content ---------------------------------------------------------------
    def _columns(self, cur, db, table):
        """(every column, the columns a copy writes). A generated column is
        computed by the server; a column with an expression default
        (``DEFAULT_GENERATED``, e.g. ``CURRENT_TIMESTAMP``) holds data and is copied."""
        cur.execute("SELECT COLUMN_NAME, GENERATION_EXPRESSION FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                    (db, table))
        rows = cur.fetchall()
        return [c for c, _ in rows], [c for c, generated in rows if not generated]

    def _digests(self, which):
        out = {}
        db = self._db(which)
        with self._session(which) as cur:
            for table, _ in self.tables:
                cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s "
                            "AND TABLE_NAME=%s", (db, table))
                if not cur.fetchone():
                    out[table] = None
                    continue
                columns = self._columns(cur, db, table)[0]
                cur.execute(f"SELECT {', '.join(name(c) for c in columns)} FROM "
                            f"{name(db)}.{name(table)}")
                out[table] = client_digest(row_text(r) for r in cur.fetchall())
        return out

    def _counters(self, which):
        """AUTO_INCREMENT position of every business table that has one."""
        tables = [t for t, _ in self.tables]
        marks = ", ".join(["%s"] * len(tables))
        return {t: n for t, n in self._rows(
            which, f"SELECT TABLE_NAME, AUTO_INCREMENT FROM information_schema.TABLES WHERE "
            f"TABLE_SCHEMA=%s AND TABLE_NAME IN ({marks}) AND AUTO_INCREMENT IS NOT NULL "
            "ORDER BY TABLE_NAME", (self._db(which), *tables))}

    def _structure(self, which):
        """Each business table's definition (columns, keys, constraints, engine,
        collation) and its triggers, without the counter or the schema name."""
        db = self._db(which)
        out = {}
        with self._session(which) as cur:
            for table, _ in self.tables:
                cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s "
                            "AND TABLE_NAME=%s", (db, table))
                if not cur.fetchone():
                    out[table] = None
                    continue
                cur.execute(f"SHOW CREATE TABLE {name(db)}.{name(table)}")
                definition = _AUTO.sub("", cur.fetchone()[1])
                cur.execute("SELECT TRIGGER_NAME, ACTION_TIMING, EVENT_MANIPULATION, "
                            "ACTION_ORDER, ACTION_STATEMENT FROM information_schema.TRIGGERS "
                            "WHERE EVENT_OBJECT_SCHEMA=%s AND EVENT_OBJECT_TABLE=%s "
                            "ORDER BY TRIGGER_NAME", (db, table))
                out[table] = [definition, [list(r) for r in cur.fetchall()]]
        return out

    # -- actions ---------------------------------------------------------------
    def _stage_target(self):
        with self._session("target") as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {name(self.target_db)}")
            cur.execute(f"USE {name(self.target_db)}")  # the application's own DDL
            for statement in statements(self.schema_sql):
                cur.execute(statement)
        self._revoke_all("target", self.writer_accounts)

    def _check_stage_target(self):
        if not self._database_exists("target"):
            return False, {"target_database": False}
        source, target = self._structure("source"), self._structure("target")
        present = all(target[t] is not None for t, _ in self.tables)
        access = self._access("target")
        same = source == target
        return present and same and not any(access.values()), {
            "tables_present": present, "same_structure": same, "target_access": access}

    def _locked(self, which, writers):
        out = {}
        for writer in writers:
            user, host = account(writer)
            row = self._rows(which, "SELECT account_locked FROM mysql.user WHERE user=%s "
                                    "AND host=%s", (user, host))
            out[writer] = None if not row else row[0][0] == "Y"
        return out

    def _pause_admission(self):
        """No new session of any writer account on the source server (sessions
        already open are ended by the fence). MySQL has no per-database
        connect right, so this is the account lock: the unit's writer accounts
        are its own, and on a server shared with the target the application
        account is unlocked again when the target is activated."""
        with self._session("source") as cur:
            for writer in self.writer_accounts:
                cur.execute(f"ALTER USER IF EXISTS {quoted(writer)} ACCOUNT LOCK")

    def _check_pause_admission(self):
        locked = self._locked("source", self.writer_accounts)
        return all(locked.values()), {"source_accounts_locked": locked}

    def _fence_source_writes(self):
        self._revoke_all("source", self.writer_accounts)
        with self._session("source") as cur:
            for session_id, _, _ in self._sessions():
                try:
                    cur.execute(f"KILL {int(session_id)}")
                except Exception as error:  # noqa: BLE001 - gone already
                    if "Unknown thread id" not in str(error):
                        raise
        deadline = time.monotonic() + self.session_wait_seconds
        while self._sessions() and time.monotonic() < deadline:
            time.sleep(0.05)

    def _check_fence_source_writes(self):
        access = self._access("source")
        writes = {w: self._writes(p) for w, p in access.items()}
        sessions = self._sessions()
        prepared = self._prepared_xa()
        return not any(writes.values()) and not sessions and not prepared, {
            "source_writes": writes, "source_sessions": sessions[:20],
            "prepared_xa_transactions": prepared[:20]}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        present, detail = self._check_stage_target()
        sessions = self._sessions_on("target")
        return present and sessions == 0, {**detail, "target_sessions": sessions}

    def _start_sync(self):
        self._copy_content()
        with self._session("target") as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {name(self.target_db)}.{name(MARKS)} "
                        "(table_name VARCHAR(64) PRIMARY KEY, copied_at DATETIME(6) NOT NULL)")
            for table, _ in self.tables:
                cur.execute(f"INSERT INTO {name(self.target_db)}.{name(MARKS)} VALUES "
                            "(%s, UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE "
                            "copied_at=VALUES(copied_at)", (table,))

    def _check_start_sync(self):
        if not self._exists("target", MARKS):
            return False, {"synced_tables": []}
        marked = {r[0] for r in self._rows(
            "target", f"SELECT table_name FROM {name(self.target_db)}.{name(MARKS)}",)}
        tables = {t for t, _ in self.tables}
        return tables <= marked, {"synced_tables": sorted(marked & tables)}

    def _apply_final_changes(self):
        self._copy_content()

    def _check_apply_final_changes(self):
        return self._check_copy_content()

    def _copy_content(self):
        source = self.connect(self.source_db)
        try:
            source.autocommit(False)
            with source.cursor() as read:
                read.execute("SET SESSION time_zone='+00:00'")
                # One consistent view of every source table.
                read.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                with self._session("target") as write:
                    write.execute("SET SESSION foreign_key_checks=0")
                    for table, key in self.tables:
                        all_columns, columns = self._columns(read, self.source_db, table)
                        target = f"{name(self.target_db)}.{name(table)}"
                        write.execute(f"DELETE FROM {target}")
                        cols = ", ".join(name(c) for c in columns)
                        read.execute(f"SELECT {cols} FROM {name(self.source_db)}.{name(table)} "
                                     f"ORDER BY {name(key)}")
                        insert = (f"INSERT INTO {target} ({cols}) VALUES "
                                  f"({', '.join(['%s'] * len(columns))})")
                        while batch := read.fetchmany(_BATCH):
                            write.executemany(insert, batch)
                    write.execute("SET SESSION foreign_key_checks=1")
            source.commit()
        finally:
            source.close()
        self._copy_counters()

    def _copy_counters(self):
        """Every AUTO_INCREMENT position, so identifiers are never issued twice."""
        with self._session("target") as cur:
            for table, position in self._counters("source").items():
                cur.execute(f"ALTER TABLE {name(self.target_db)}.{name(table)} "
                            f"AUTO_INCREMENT = {int(position)}")

    def _check_copy_content(self):
        source, target = self._digests("source"), self._digests("target")
        rows = {t: [(source[t] or {}).get("rows"), (target[t] or {}).get("rows")]
                for t in source}
        counters = self._counters("source"), self._counters("target")
        return (all(a == b is not None for a, b in rows.values())
                and counters[0] == counters[1]), {"rows": rows, "counters": list(counters)}

    def _verify_final_state(self):
        return None  # measurement only; the readback is the comparison

    def _check_verify_final_state(self):
        source, target = self._digests("source"), self._digests("target")
        counters = self._counters("source"), self._counters("target")
        return (source == target and None not in source.values()
                and counters[0] == counters[1]), {
            "source": source, "target": target,
            "counters": {"source": counters[0], "target": counters[1]}}

    def _activate_target(self):
        with self._session("target") as cur:
            cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {name(self.target_db)}.* "
                        f"TO {quoted(self.app_account)}")
            # Only the application account; on the source server it stays without
            # any right on the source database (the fence), locked or not.
            cur.execute(f"ALTER USER {quoted(self.app_account)} ACCOUNT UNLOCK")

    def _check_activate_target(self):
        target = self._access("target")
        app = set(target[self.app_account])
        needed = {f"database:{p}" for p in ("SELECT", "INSERT", "UPDATE", "DELETE")}
        unlocked = self._locked("target", (self.app_account,))[self.app_account] is False
        others = {w: self._writes(p) for w, p in target.items() if w != self.app_account}
        source = {w: self._writes(p) for w, p in self._access("source").items()}
        return (needed <= app and unlocked and not any(others.values())
                and not any(source.values())), {
            "target_access": target, "app_account_unlocked": unlocked,
            "source_writes": source}

    def _commit_route(self):
        self.route.set(self.target_db)

    def _check_commit_route(self):
        current = self.route.get()
        return current == self.target_db, {"route": current}


_LOOP_ENDS = frozenset({"IF", "WHILE", "LOOP", "REPEAT"})
_WORD = re.compile(r"\w+")


def statements(script):
    """Split a DDL script on ``;`` outside quotes, comments and compound
    statements (``BEGIN ... END`` bodies with ``END IF`` and the like inside,
    ``CASE ... END CASE`` statements, ``CASE ... END`` expressions), as the
    mysql client does with a changed DELIMITER."""
    tokens = re.findall(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`|--[^\n]*|#[^\n]*"
                        r"|/\*.*?\*/|\w+|;|\s+|.", script, re.S)

    def adjacent(i, step):
        """The word right before/after token i (only whitespace between), upper-cased."""
        j = i + step
        while 0 <= j < len(tokens) and tokens[j].isspace():
            j += step
        return tokens[j].upper() if 0 <= j < len(tokens) and _WORD.fullmatch(tokens[j]) \
            else None

    out, current, open_ = [], [], []
    for i, token in enumerate(tokens):
        word = token.upper() if _WORD.fullmatch(token) else None
        if token == ";" and not open_:
            statement = "".join(current).strip()
            if statement:
                out.append(statement)
            current = []
            continue
        if word == "BEGIN":
            open_.append("BEGIN")
        elif word == "CASE" and adjacent(i, -1) != "END":
            open_.append("CASE")
        elif word == "END" and open_:
            after = adjacent(i, 1)
            if after == "CASE":
                while open_ and open_.pop() != "CASE":
                    pass
            elif after not in _LOOP_ENDS:
                open_.pop()
        current.append(token)
    statement = "".join(current).strip()
    if statement:
        out.append(statement)
    return out
