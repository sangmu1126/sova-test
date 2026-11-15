"""PostgreSQL snapshot + change data capture by logical replication (§16.1, §16.2, §16.5).

The same handoff steps as the offline-copy reference adapter, except that the
data moves before the fence and only the remaining changes are applied inside
it:

* start_sync - checks that every unit table can publish updates and deletes
  (a primary key, a replica identity index or REPLICA IDENTITY FULL; otherwise
  publishing would break the application's UPDATE and DELETE on the source),
  then creates a publication of exactly the unit's tables and a logical slot
  on the source, and a subscription on the target that copies the tables and
  then follows every committed change. It returns once every table is ready.
  Nothing is created when a check fails; the readback says why.
* apply_final_changes - after the fence, waits until the subscription has
  confirmed the source's WAL position taken after the fence (same publisher,
  same slot: §16.2's "final checkpoint applied", not a lag figure), copies the
  sequence positions (logical replication does not carry them), and removes
  the subscription, the slot and the publication. If the position is not
  confirmed in time nothing is removed, so the operator can repair and retry.
* every other step is the offline-copy adapter's.

The readback of ``apply_final_changes`` holds only when the content of every
table and every sequence position are equal and no replication object is
left: the target has no second writer when it is activated, and the source
keeps no slot that retains WAL.

A handoff aborted after ``start_sync`` must call ``release_replication()``;
``replication_released()`` reads back that nothing is left. Until then the slot
retains source WAL (bounded only by the server's ``max_slot_wal_keep_size``).

``source_conninfo`` is how the *target server* reaches the source; it is stored
in the target's subscription catalog, so it should carry no password (use a
password file or certificate). It never appears in evidence.
"""

from contextlib import contextmanager
import hashlib
import time

from psycopg import sql

from sova.adapters.postgres_offline_copy import PostgresOfflineCopyAdapter
from sova.domain.contracts import ContractError

_READY = "r"


def replication_name(source_db, target_db):
    """One deterministic name per direction for the publication, slot and subscription."""
    digest = hashlib.sha256(f"{source_db}\0{target_db}".encode()).hexdigest()
    return "sova_cdc_" + digest[:24]


class PostgresLogicalReplicationAdapter(PostgresOfflineCopyAdapter):
    def __init__(self, adapter_id, *, source_conninfo, sync_wait_seconds=120.0,
                 final_sync_wait_seconds=30.0, **kwargs):
        super().__init__(adapter_id, **kwargs)
        if type(source_conninfo) is not str or not source_conninfo.strip():
            raise ContractError("The source connection as seen from the target required")
        self.source_conninfo = source_conninfo
        self.sync_wait_seconds = sync_wait_seconds
        self.final_sync_wait_seconds = final_sync_wait_seconds
        self.name = replication_name(self.source_db, self.target_db)

    # -- observations ----------------------------------------------------------
    @contextmanager
    def _autocommit(self, which):
        """A connection outside a transaction block (publication, subscription
        and slot commands), whatever the connection factory gives."""
        with self.connect(self._db(which)) as conn:
            conn.autocommit = True
            yield conn

    def _wal_level(self):
        with self.connect(self.source_db) as conn:
            return conn.execute("SHOW wal_level").fetchone()[0]

    def _without_identity(self):
        """Unit tables whose updates and deletes cannot be published."""
        names = [t for t, _ in self.tables]
        with self.connect(self.source_db) as conn:
            rows = conn.execute(
                "SELECT c.relname, c.relreplident, "
                "EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary), "
                "EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid "
                "        AND i.indisreplident) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY(%s)", (names,)).fetchall()
        found = {name: (ident == "f" or (ident == "d" and pk) or (ident == "i" and index))
                 for name, ident, pk, index in rows}
        return sorted(n for n in names if not found.get(n, False))

    def _publication(self):
        with self.connect(self.source_db) as conn:
            if not conn.execute("SELECT 1 FROM pg_publication WHERE pubname=%s",
                                (self.name,)).fetchone():
                return None
            return sorted(r[0] for r in conn.execute(
                "SELECT tablename FROM pg_publication_tables WHERE pubname=%s "
                "AND schemaname='public'", (self.name,)))

    def _slot(self):
        with self.connect(self.source_db) as conn:
            row = conn.execute(
                "SELECT active, confirmed_flush_lsn::text FROM pg_replication_slots "
                "WHERE slot_name=%s AND database=%s", (self.name, self.source_db)).fetchone()
        return None if row is None else {"active": row[0], "confirmed_flush_lsn": row[1]}

    def _subscription(self):
        with self.connect(self.target_db) as conn:
            row = conn.execute(
                "SELECT s.oid, s.subenabled, s.subslotname, s.subpublications "
                "FROM pg_subscription s JOIN pg_database d ON d.oid = s.subdbid "
                "WHERE s.subname=%s AND d.datname=%s", (self.name, self.target_db)).fetchone()
            if row is None:
                return None
            states = dict(conn.execute(
                "SELECT c.relname, r.srsubstate FROM pg_subscription_rel r "
                "JOIN pg_class c ON c.oid = r.srrelid WHERE r.srsubid=%s", (row[0],)).fetchall())
        return {"enabled": row[1], "slot": row[2], "publications": list(row[3]),
                "tables": {t: states.get(t) for t, _ in self.tables}}

    def replication_released(self):
        """``(released, detail)``: no publication, slot or subscription of this handoff."""
        publication, slot, subscription = self._publication(), self._slot(), self._subscription()
        return publication is None and slot is None and subscription is None, {
            "publication": publication is not None, "slot": slot is not None,
            "subscription": subscription is not None}

    # -- start_sync --------------------------------------------------------------
    def _start_sync(self):
        if self._wal_level() != "logical" or self._without_identity():
            return  # nothing is created; the readback reports why
        tables = sql.SQL(", ").join(sql.Identifier(t) for t, _ in self.tables)
        with self._autocommit("source") as source:
            if self._publication() is None:
                source.execute(sql.SQL("CREATE PUBLICATION {} FOR TABLE {}").format(
                    sql.Identifier(self.name), tables))
            if self._slot() is None:
                # Creating a slot waits for running transactions; never hang on one.
                source.execute(sql.SQL("SET statement_timeout = {}").format(
                    sql.Literal(int(self.sync_wait_seconds * 1000))))
                source.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')",
                               (self.name,))
        if self._subscription() is None:
            with self.connect(self.target_db) as target:
                for table, _ in self.tables:  # a staged target holds no business data
                    target.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(table)))
            with self._autocommit("target") as target:
                # The slot exists already: a subscription to the same cluster may
                # not create it, and the slot must predate the initial copy.
                target.execute(sql.SQL(
                    "CREATE SUBSCRIPTION {} CONNECTION {} PUBLICATION {} WITH "
                    "(create_slot = false, slot_name = {}, copy_data = true, "
                    "enabled = true)").format(
                        sql.Identifier(self.name), sql.Literal(self.source_conninfo),
                        sql.Identifier(self.name), sql.Literal(self.name)))
        deadline = time.monotonic() + self.sync_wait_seconds
        while time.monotonic() < deadline:
            subscription = self._subscription()
            if subscription and all(s == _READY for s in subscription["tables"].values()):
                return
            time.sleep(0.1)

    def _check_start_sync(self):
        wal_level, missing = self._wal_level(), self._without_identity()
        publication, slot, subscription = self._publication(), self._slot(), self._subscription()
        tables = sorted(t for t, _ in self.tables)
        holds = (wal_level == "logical" and not missing and publication == tables
                 and slot is not None and subscription is not None
                 and subscription["enabled"] and subscription["slot"] == self.name
                 and subscription["publications"] == [self.name]
                 and all(s == _READY for s in subscription["tables"].values()))
        return holds, {
            "wal_level": wal_level, "tables_without_replica_identity": missing,
            "published_tables": publication, "slot": slot is not None,
            "subscription_tables": None if subscription is None else subscription["tables"]}

    # -- apply_final_changes -----------------------------------------------------
    def _caught_up(self, position):
        with self.connect(self.source_db) as conn:
            row = conn.execute(
                "SELECT confirmed_flush_lsn >= %s::pg_lsn FROM pg_replication_slots "
                "WHERE slot_name=%s", (position, self.name)).fetchone()
        return bool(row and row[0])

    def _apply_final_changes(self):
        if self._subscription() is not None:
            with self.connect(self.source_db) as conn:
                position = conn.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0]
            deadline = time.monotonic() + self.final_sync_wait_seconds
            while not self._caught_up(position):
                if time.monotonic() >= deadline:
                    return  # left running for repair; the readback stays UNKNOWN
                time.sleep(0.05)
        with self.connect(self.target_db) as target:
            self._copy_sequences(target)
        self.release_replication()

    def release_replication(self):
        """Remove this handoff's subscription, slot and publication (idempotent)."""
        if self._subscription() is not None:
            with self._autocommit("target") as target:
                name = sql.Identifier(self.name)
                target.execute(sql.SQL("ALTER SUBSCRIPTION {} DISABLE").format(name))
                # Detach the slot so dropping does not reach the source; the
                # slot is dropped below once its sender has gone.
                target.execute(sql.SQL("ALTER SUBSCRIPTION {} SET (slot_name = NONE)").format(
                    name))
                target.execute(sql.SQL("DROP SUBSCRIPTION {}").format(name))
        deadline = time.monotonic() + self.session_wait_seconds
        while (slot := self._slot()) is not None:
            if not slot["active"]:
                with self.connect(self.source_db) as source:
                    source.execute(
                        "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
                        "WHERE slot_name=%s AND NOT active", (self.name,))
                continue
            if time.monotonic() >= deadline:
                break  # still attached; the readback reports the slot
            time.sleep(0.05)
        with self.connect(self.source_db) as source:
            source.execute(sql.SQL("DROP PUBLICATION IF EXISTS {}").format(
                sql.Identifier(self.name)))

    def _check_apply_final_changes(self):
        source, target = self._digests("source"), self._digests("target")
        sequences = self._sequences("source"), self._sequences("target")
        released, detail = self.replication_released()
        rows = {t: [source[t]["rows"], target[t]["rows"]] for t in source}
        return source == target and sequences[0] == sequences[1] and released, {
            "rows": rows, "content_equal": source == target,
            "sequences_equal": sequences[0] == sequences[1], "replication_left": detail}
