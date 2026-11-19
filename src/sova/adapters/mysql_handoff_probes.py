"""Independent probes for a MySQL handoff (§16.3, §15.5, §28.2).

The same measurements as the PostgreSQL probes, made separately from the
adapter that changed the databases:

* write denial needs two views to agree (ADR-0007): an attempt - each
  registered writer account connects and makes a real write inside a
  transaction that is always rolled back - and the server's own list of the
  account's sessions. MySQL leaves an open session its global privileges
  after a ``REVOKE`` and its login after the account is locked, so a refused
  connection, a locked account or a refused statement is a denial only when
  no session of the account is open; an error outside those decides
  nothing;
* content probes compare each table's rows as a multiset (the shared row
  digest over a canonical row text, session time zone pinned to UTC);
* the restore drill rebuilds the authoritative tables in an empty database on
  the target server (``CREATE TABLE ... LIKE``, ``INSERT ... SELECT``) and
  compares;
* transport encryption is this session's own channel: the server reports the
  session's TLS version, and this client must have verified the certificate
  chain and host name;
* placement: MySQL has no database comment to carry a label, so the zone is
  read through ``zone_of(database)`` - the provider's view of where the
  server runs (an RDS instance's zone, a Cloud SQL instance's region, an
  on-premises inventory). It is reported as read, not assumed.

Each probe returns ``(passed, observation)``.
"""

import ssl

from sova.adapters.mysql_offline_copy import name, row_text
from sova.adapters.row_digest import client_digest
from sova.adapters.tls_refusal import check_tls, mysql_plaintext, side_refusals
from sova.domain.contracts import ContractError

DENIED = {1044, 1045, 1049, 1142, 1143, 1227, 1370, 3118}  # access, database, command, locked


def _code(error):
    return getattr(error, "args", (None,))[0]


class MySqlHandoffProbes:
    def __init__(self, *, connect_as, admin_connect, source_db, target_db, app_account,
                 tables, probe_insert, writer_accounts=None, zone_of=None, tls=None):
        """``connect_as(database, account)`` opens a session as a writer account
        with ``database`` selected; ``admin_connect(database)`` an administrative
        session on the server holding ``database``, none selected."""
        if not callable(connect_as) or not callable(admin_connect):
            raise ContractError("Writer and administrative connections required")
        self.connect_as, self.admin_connect = connect_as, admin_connect
        self.tls = check_tls(tls, "mysql")
        self.source_db, self.target_db, self.app_account = source_db, target_db, app_account
        self.tables, self.probe_insert = tables, probe_insert
        self.writer_accounts = tuple(writer_accounts) if writer_accounts else (app_account,)
        self.zone_of = zone_of

    def _db(self, which):
        return self.source_db if which == "source" else self.target_db

    def _try_write(self, which, writer=None, *, database=None):
        """True when a real write by ``writer`` (default: the application) would commit."""
        import pymysql

        try:
            conn = self.connect_as(database or self._db(which), writer or self.app_account)
        except pymysql.err.MySQLError as error:
            if _code(error) in DENIED:
                return False, "connection_refused"
            raise
        try:
            with conn.cursor() as cur:
                cur.execute(self.probe_insert)
            return True, "write_accepted"
        except pymysql.err.MySQLError as error:
            if _code(error) in DENIED:
                return False, "write_privilege_denied"
            raise
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def _sessions(self, which, writer):
        """The account's open sessions on the server, as the server lists them."""
        user = writer.rsplit("@", 1)[0]
        conn = self.admin_connect(self._db(which))
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM information_schema.PROCESSLIST "
                            "WHERE USER = %s", (user,))
                return cur.fetchone()[0]
        finally:
            conn.close()

    def _denied(self, which, writer):
        wrote, attempt = self._try_write(which, writer)
        sessions = self._sessions(which, writer)
        denied = not wrote and attempt in ("connection_refused", "write_privilege_denied") \
            and sessions == 0
        return denied, {"attempt": attempt, "open_sessions": sessions}

    def write_denied(self, which):
        outcomes = {w: self._denied(which, w) for w in self.writer_accounts}
        return all(d for d, _ in outcomes.values()), {
            "probe": "write_denied", "database": self._db(which),
            "outcomes": {w: detail for w, (_, detail) in outcomes.items()}}

    def cold_start_without_source(self):
        """A fresh application session writes on the target while the source
        cannot be reached at all (independence drill, §20.1, AT-35)."""
        import pymysql

        try:
            self.connect_as(self.source_db, self.app_account).close()
            source = "reachable"
        except pymysql.err.MySQLError as error:
            if _code(error) not in DENIED:
                raise
            source = "unreachable"
        wrote, outcome = self._try_write("target")
        return source == "unreachable" and wrote, {
            "probe": "cold_start_without_source", "source": source, "target": outcome}

    def single_writer(self):
        target, target_outcome = self._try_write("target")
        source_denied, source = self.write_denied("source")
        return target and source_denied, {"probe": "single_writer", "target": target_outcome,
                                          "source": source["outcomes"]}

    def _digest(self, database, table, *, server=None):
        """``server``: the database whose server holds ``database`` (default itself)."""
        conn = self.admin_connect(server or database)
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION time_zone='+00:00'")
                cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE "
                            "TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                            (database, table))
                columns = [c for (c,) in cur.fetchall()]
                if not columns:
                    return None
                cur.execute(f"SELECT {', '.join(name(c) for c in columns)} FROM "
                            f"{name(database)}.{name(table)}")
                return client_digest(row_text(r) for r in cur.fetchall())
        finally:
            conn.rollback()
            conn.close()

    def content_equal(self, table):
        if table not in dict(self.tables):
            raise ContractError("Unregistered table")
        source = self._digest(self.source_db, table)
        target = self._digest(self.target_db, table)
        return source == target is not None, {"probe": "content_equal", "table": table,
                                              "source": source, "target": target}

    def order_effects_once(self):
        """ORDER-01: one confirmed order per customer and idempotency key, each
        with its recorded result, on the database that now holds authority."""
        db = name(self.target_db)
        conn = self.admin_connect(self.target_db)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM (SELECT customer, idempotency_key FROM "
                            f"{db}.orders GROUP BY 1, 2 HAVING count(*) > 1) d")
                duplicates = cur.fetchone()[0]
                cur.execute(f"SELECT count(*) FROM {db}.orders o LEFT JOIN "
                            f"{db}.idempotency_results r USING (customer, idempotency_key) "
                            "WHERE r.customer IS NULL")
                unrecorded = cur.fetchone()[0]
        finally:
            conn.rollback()
            conn.close()
        return duplicates == 0 and unrecorded == 0, {
            "probe": "order_effects_once", "duplicates": duplicates, "unrecorded": unrecorded}

    def zone(self, which, allowed):
        label = self.zone_of(self._db(which)) if self.zone_of else None
        return label in allowed, {"probe": "zone", "database": self._db(which),
                                  "label": label, "allowed": sorted(allowed),
                                  "source": "provider" if self.zone_of else "none"}

    def transport_refusals(self, which):
        """The side's registered TLS endpoint refuses a wrong CA, a wrong host
        name, and the application account's login without TLS (no password
        sent)."""
        return side_refusals(self.tls, which,
                             mysql_plaintext(self.app_account.rsplit("@", 1)[0]))

    def transport_encrypted(self, which):
        conn = self.admin_connect(self._db(which))
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW SESSION STATUS LIKE 'Ssl_version'")
                row = cur.fetchone()
            context = getattr(conn, "ctx", None)
            verified_chain = bool(context and context.verify_mode == ssl.CERT_REQUIRED
                                  and context.check_hostname)
        finally:
            conn.close()
        version = row[1] if row and row[1] else None
        return bool(version) and verified_chain, {
            "probe": "transport_encrypted", "encrypted": bool(version),
            "tls_version": version, "client_verification":
                "verify-identity" if verified_chain else "none"}

    def cross_tenant_denied(self, other_db):
        import pymysql

        try:
            conn = self.connect_as(other_db, self.app_account)
        except pymysql.err.MySQLError as error:
            if _code(error) in DENIED:
                return True, {"probe": "cross_tenant_denied", "outcome": "connection_refused"}
            raise
        conn.close()
        return False, {"probe": "cross_tenant_denied", "outcome": "connected"}

    def restore_drill(self, scratch_db):
        """Rebuild the authoritative tables in an empty database on the target's
        server and compare them with the target."""
        target = name(self.target_db)
        conn = self.admin_connect(self.target_db)
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS {name(scratch_db)}")
                for table, _ in self.tables:
                    scratch = f"{name(scratch_db)}.{name(table)}"
                    cur.execute(f"DROP TABLE IF EXISTS {scratch}")
                    cur.execute(f"CREATE TABLE {scratch} LIKE {target}.{name(table)}")
                    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE "
                                "TABLE_SCHEMA=%s AND TABLE_NAME=%s AND GENERATION_EXPRESSION="
                                "'' ORDER BY ORDINAL_POSITION", (self.target_db, table))
                    columns = ", ".join(name(c) for (c,) in cur.fetchall())
                    cur.execute(f"INSERT INTO {scratch} ({columns}) SELECT {columns} FROM "
                                f"{target}.{name(table)}")
            conn.commit()
        finally:
            conn.close()
        # The scratch database was made on the target's server: read it there.
        restored = {table: self._digest(self.target_db, table) ==
                    self._digest(scratch_db, table, server=self.target_db)
                    for table, _ in self.tables}
        return all(restored.values()), {"probe": "restore_drill", "tables": restored}
