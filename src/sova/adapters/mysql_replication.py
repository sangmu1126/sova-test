"""Reference adapter: MySQL snapshot + change capture by the server's own
replication (§16.1 snapshot-cdc), between two servers.

Everything about writers, the fence, activation and comparison is the offline
copy adapter's. What changes is when the data moves, so the write stop no
longer grows with the data:

* ``start_sync`` takes one consistent snapshot of the source together with
  its binary-log position: a global read lock for the instant it takes to
  open the snapshot and read the position (as ``mysqldump`` does), then the
  copy runs from the snapshot while the source keeps serving. The target
  server then replicates from exactly that position, through a replication
  channel of the unit's own, filtered to the source database and rewriting
  its name to the target database. Rows arrive as row events: triggers do
  not fire twice.
* ``apply_final_changes``, once the source is fenced, reads the source's
  position, waits (bounded) until the channel has applied everything before
  it, removes the channel and copies the ``AUTO_INCREMENT`` counters (a
  number issued and withdrawn on the source is not issued again). It reads
  back as done only when the channel is gone and the content and counters
  are equal. A plan whose sync never ran gets a full copy here instead.
* ``release_replication`` removes the channel on an abort (the source keeps
  its binary logs for a channel that reads them; a forgotten channel would
  also keep writing into the target).

The target server reaches the source at ``source_endpoint`` (as the target
sees it) with a replication account the operator created on the source
(``REPLICATION SLAVE``); its password comes from the worker's secrets. With
``replication_tls`` the channel requires TLS and verifies the source's
certificate and host name against ``replication_ca_path``, a CA file on the
target server.

``snapshot="lockless"`` is for sources that grant no global read lock, as
managed services (RDS, Cloud SQL) do not. The binary-log position is read
first, a short settle lets every transaction whose log was already written
finish its engine commit, and only then the snapshot opens. Changes between
that position and the snapshot are in the copy and come again from the
channel, so the target server applies them idempotently
(``replica_exec_mode=IDEMPOTENT``: an insert of a row it has, or a change of
a row it lacks, is not an error; row events carry whole rows, so applying
them in order ends where the source is). Once the channel has passed the
position read right after the snapshot opened, everything it applies is
after the snapshot and the mode goes back to ``STRICT``; the readback
confirms that. It needs a primary key on every table (a duplicate row of a
table without one would be kept), and ``replica_exec_mode`` is global on
the target server: it should serve no other channel meanwhile. The window
is also applied by one thread: the multi-threaded applier took about a
second per transaction it found already applied (measured: 69 s against
0.4 s for 60). Both settings are saved in the target database before they
change and put back from there, so a worker lost in between does not leave
the operator's settings behind. A transaction the settle did not cover
would be missing from the target, and the final comparison would hold the
handoff.

The two servers' ``gtid_mode`` need not match. A target with GTIDs ``ON``
refuses an anonymous transaction, which is all a source with them ``OFF``
(MySQL's own default) writes: the channel then gives each one a GTID of its
own (``ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS``) under a UUID derived from
the channel, so the target's ``gtid_executed`` tells the handed-off
transactions from its own. A target with GTIDs ``OFF`` cannot take a
transaction that has one: a source that writes them (``ON``,
``ON_PERMISSIVE``) is refused before the snapshot, naming the target modes
that would take it (both permissive modes, reached online).
"""

import hashlib
import json
import re
import time
import uuid

from sova.adapters.mysql_offline_copy import MARKS, MySqlOfflineCopyAdapter, account, name
from sova.domain.contracts import ContractError

_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
# Client errors the channel recovers from by connecting again: cannot reach the
# host (2002, 2003), cannot resolve it yet (2005), connection lost (2013).
_TRANSIENT = frozenset({2002, 2003, 2005, 2013})
_CONNECT_RETRY_SECONDS = 5  # the server's default is 60
_APPLIER = "sova_handoff_applier"  # the target's own applier settings, while changed
# What a server with each gtid_mode writes, and what it takes from a channel.
_WRITES_GTIDS = frozenset({"ON_PERMISSIVE", "ON"})
_TAKES_ANONYMOUS = frozenset({"OFF", "OFF_PERMISSIVE", "ON_PERMISSIVE"})
_TAKES_GTIDS = frozenset({"OFF_PERMISSIVE", "ON_PERMISSIVE", "ON"})


class MySqlReplicationAdapter(MySqlOfflineCopyAdapter):
    def __init__(self, adapter_id, *, source_endpoint, replication_account,
                 replication_password, replication_tls=False, replication_ca_path=None,
                 catch_up_seconds=120, snapshot="locked", settle_seconds=1.0, **common):
        super().__init__(adapter_id, **common)
        if snapshot not in ("locked", "lockless"):
            raise ContractError("snapshot: locked or lockless")
        if type(settle_seconds) not in (int, float) or not 0 <= settle_seconds <= 30:
            raise ContractError("settle_seconds: 0 to 30")
        self.snapshot_mode, self.settle_seconds = snapshot, settle_seconds
        host, port = source_endpoint if type(source_endpoint) is tuple else (None, None)
        if type(host) is not str or not _HOST.fullmatch(host) or type(port) is not int:
            raise ContractError("The source as the target server reaches it: (host, port)")
        account(replication_account)
        if type(replication_password) is not str or not replication_password:
            raise ContractError("The replication account's password required")
        self.source_endpoint, self.replication_account = (host, port), replication_account
        if replication_tls and (type(replication_ca_path) is not str or
                                not replication_ca_path.startswith("/")):
            raise ContractError("TLS replication verifies against a CA file on the target "
                                "server: its absolute path required")
        self.replication_password, self.replication_tls = replication_password, replication_tls
        self.replication_ca_path = replication_ca_path
        self.catch_up_seconds = catch_up_seconds
        self.channel = "sova_" + hashlib.sha256(json.dumps(
            [adapter_id, self.source_db, self.target_db]).encode()).hexdigest()[:24]
        # The GTIDs the channel gives anonymous transactions, when the target needs them.
        self.assigned_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "sova:mysql:" + self.channel))

    def _gtid_modes(self):
        modes = {}
        for side in ("source", "target"):
            with self._session(side) as cur:
                cur.execute("SELECT @@GLOBAL.gtid_mode")
                modes[side] = cur.fetchone()[0]
        return modes

    def _assign_gtids(self, modes):
        """Whether the channel must give anonymous transactions GTIDs; refused if
        the target cannot take the source's own GTIDs."""
        if modes["source"] in _WRITES_GTIDS and modes["target"] not in _TAKES_GTIDS:
            raise ContractError(
                f"The target (gtid_mode {modes['target']}) cannot apply the source's GTID "
                f"transactions (gtid_mode {modes['source']}): set the target's gtid_mode "
                "to OFF_PERMISSIVE or ON_PERMISSIVE first")
        return modes["source"] not in _WRITES_GTIDS and modes["target"] not in _TAKES_ANONYMOUS \
            or modes["source"] == "ON_PERMISSIVE" and modes["target"] == "ON"

    def _assigned(self):
        """The channel's own setting, as the target server reports it."""
        with self._session("target") as cur:
            cur.execute("SELECT ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS_TYPE, "
                        "ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS_VALUE FROM performance_schema."
                        "replication_applier_configuration WHERE CHANNEL_NAME = %s",
                        (self.channel,))
            row = cur.fetchone()
        return None if row is None else {"type": row[0], "value": row[1] or None}

    def _behind(self, what, file, position):
        status = self._status() or {}
        return ContractError(
            f"The replica did not reach {what} {file}:{position} in time: read "
            f"{status.get('Source_Log_File')}:{status.get('Read_Source_Log_Pos')}, executed "
            f"{status.get('Relay_Source_Log_File')}:{status.get('Exec_Source_Log_Pos')}, "
            f"io={status.get('Last_IO_Error') or None!r} "
            f"sql={status.get('Last_SQL_Error') or None!r}")

    # -- the channel -----------------------------------------------------------------
    def _status(self):
        """The channel's replica status as a dict, or None if it does not exist."""
        import pymysql

        with self._session("target") as cur:
            try:
                cur.execute("SHOW REPLICA STATUS FOR CHANNEL %s", (self.channel,))
            except pymysql.err.MySQLError as error:
                if getattr(error, "args", (None,))[0] == 3074:  # no such channel
                    return None
                raise
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip((d[0] for d in cur.description), row))

    def _drop_channel(self):
        if self._status() is None:
            return
        with self._session("target") as cur:
            cur.execute("STOP REPLICA FOR CHANNEL %s", (self.channel,))
            cur.execute("RESET REPLICA ALL FOR CHANNEL %s", (self.channel,))

    def _snapshot(self):
        """Copy the source as of one binary-log position; returns (file, position)."""
        lock, snapshot = self.connect(self.source_db), self.connect(self.source_db)
        try:
            snapshot.autocommit(False)
            with lock.cursor() as hold, snapshot.cursor() as read:
                read.execute("SET SESSION time_zone='+00:00'")
                hold.execute("FLUSH TABLES WITH READ LOCK")
                try:  # nothing commits on the source while the two are taken together
                    read.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                    hold.execute("SHOW BINARY LOG STATUS")
                    file, position = hold.fetchone()[:2]
                finally:
                    hold.execute("UNLOCK TABLES")
                self._copy_rows(read)
            snapshot.commit()
        finally:
            lock.close()
            snapshot.close()
        return file, int(position)

    def _snapshot_lockless(self):
        """Copy the source without a global read lock; returns the position the
        channel starts from and the one after which nothing is in the copy."""
        with self._session("source") as cur:
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLE_CONSTRAINTS "
                        "WHERE TABLE_SCHEMA=%s AND CONSTRAINT_TYPE='PRIMARY KEY'",
                        (self.source_db,))
            keyed = {r[0] for r in cur.fetchall()}
        lacking = sorted(t for t, _ in self.tables if t not in keyed)
        if lacking:
            raise ContractError(f"A lockless snapshot needs a primary key on {lacking}")
        snapshot = self.connect(self.source_db)
        try:
            snapshot.autocommit(False)
            with snapshot.cursor() as read:
                read.execute("SET SESSION time_zone='+00:00'")
                read.execute("SHOW BINARY LOG STATUS")
                start = tuple(read.fetchone()[:2])
                time.sleep(self.settle_seconds)  # logged commits finish in the engine
                read.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                read.execute("SHOW BINARY LOG STATUS")
                after = tuple(read.fetchone()[:2])
                self._copy_rows(read)
            snapshot.commit()
        finally:
            snapshot.close()
        return (start[0], int(start[1])), (after[0], int(after[1]))

    def _applier_table(self, cur):
        cur.execute(f"CREATE TABLE IF NOT EXISTS {name(self.target_db)}.{_APPLIER} "
                    "(setting VARCHAR(64) PRIMARY KEY, value VARCHAR(64) NOT NULL)")
        return f"{name(self.target_db)}.{_APPLIER}"

    def _open_window(self):
        """Idempotent, single-threaded application of the copy's window; the
        target's own settings saved first (a rerun keeps the first ones)."""
        with self._session("target") as cur:
            table = self._applier_table(cur)
            cur.execute("SELECT @@GLOBAL.replica_parallel_workers, @@GLOBAL.replica_exec_mode")
            workers, mode = cur.fetchone()
            cur.execute(f"INSERT IGNORE INTO {table} VALUES ('replica_parallel_workers', %s), "
                        "('replica_exec_mode', %s)", (str(workers), mode))
            cur.execute("SET GLOBAL replica_parallel_workers = 1")
            cur.execute("SET GLOBAL replica_exec_mode = 'IDEMPOTENT'")

    def _saved(self, cur):
        """The target's own applier settings as the window saved them ({} when
        no window is open); reads only."""
        cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s "
                    "AND TABLE_NAME=%s", (self.target_db, _APPLIER))
        if not cur.fetchone():
            return {}
        cur.execute(f"SELECT setting, value FROM {name(self.target_db)}.{_APPLIER}")
        return dict(cur.fetchall())

    def _close_window(self):
        """Put the target's own applier settings back, as saved."""
        with self._session("target") as cur:
            saved = self._saved(cur)
            if not saved:
                return
            table = f"{name(self.target_db)}.{_APPLIER}"
            if "replica_parallel_workers" in saved:
                cur.execute("SET GLOBAL replica_parallel_workers = %s",
                            (int(saved["replica_parallel_workers"]),))
            if saved.get("replica_exec_mode") in ("STRICT", "IDEMPOTENT"):
                cur.execute(f"SET GLOBAL replica_exec_mode = '{saved['replica_exec_mode']}'")
            cur.execute(f"DELETE FROM {table}")

    def _window(self):
        """(open, the target's exec mode, its applier threads) as they are."""
        with self._session("target") as cur:
            open_ = bool(self._saved(cur))
            cur.execute("SELECT @@GLOBAL.replica_exec_mode, @@GLOBAL.replica_parallel_workers")
            mode, workers = cur.fetchone()
        return open_, mode, workers

    def _strict_after(self, file, position):
        """Wait (bounded) until the channel has applied everything up to the
        position after which nothing is in the copy, then put the target's own
        applier settings back."""
        with self._session("target") as cur:
            cur.execute("SELECT SOURCE_POS_WAIT(%s, %s, %s, %s)",
                        (file, position, self.catch_up_seconds, self.channel))
            reached = cur.fetchone()[0]
        if reached is None or reached < 0:
            raise self._behind("the snapshot's position", file, position)
        with self._session("target") as cur:
            cur.execute("STOP REPLICA SQL_THREAD FOR CHANNEL %s", (self.channel,))
        self._close_window()  # the applier threads are read when it starts again
        with self._session("target") as cur:
            cur.execute("START REPLICA SQL_THREAD FOR CHANNEL %s", (self.channel,))

    def _start_channel(self, file, position, assign):
        user, _ = account(self.replication_account)
        host, port = self.source_endpoint
        tls = (", SOURCE_SSL=1, SOURCE_SSL_VERIFY_SERVER_CERT=1, SOURCE_SSL_CA=%s"
               if self.replication_tls else "")
        gtids = ", ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS=%s" if assign else ""
        with self._session("target") as cur:
            cur.execute(
                "CHANGE REPLICATION SOURCE TO SOURCE_HOST=%s, SOURCE_PORT=%s, SOURCE_USER=%s, "
                "SOURCE_PASSWORD=%s, SOURCE_LOG_FILE=%s, SOURCE_LOG_POS=%s, "
                f"SOURCE_AUTO_POSITION=0, GET_SOURCE_PUBLIC_KEY=1, "
                f"SOURCE_CONNECT_RETRY={_CONNECT_RETRY_SECONDS}{tls}{gtids} FOR CHANNEL %s",
                (host, port, user, self.replication_password, file, position,
                 *((self.replication_ca_path,) if self.replication_tls else ()),
                 *((self.assigned_uuid,) if assign else ()), self.channel))
            cur.execute(
                f"CHANGE REPLICATION FILTER REPLICATE_REWRITE_DB = (({name(self.source_db)}, "
                f"{name(self.target_db)})), REPLICATE_DO_DB = ({name(self.target_db)}) "
                "FOR CHANNEL %s", (self.channel,))
            cur.execute("START REPLICA FOR CHANNEL %s", (self.channel,))

    def _await_channel(self, seconds=30):
        """``START REPLICA`` returns before the channel has reached the source; a
        TLS handshake under load takes seconds, and a source just reachable may
        refuse the first connection. Wait (bounded) until the channel runs or
        reports an error that retrying will not cure (TLS verification, a
        refused login), so the readback that follows sees where it settled."""
        deadline = time.monotonic() + min(seconds, self.catch_up_seconds)
        while time.monotonic() < deadline:
            status = self._status()
            if status is None or status.get("Replica_IO_Running") == "Yes":
                return
            errno = int(status.get("Last_IO_Errno") or 0)
            if errno and errno not in _TRANSIENT:
                return
            time.sleep(0.2)

    # -- actions ---------------------------------------------------------------
    def _start_sync(self):
        self._drop_channel()  # a second run starts over from a new snapshot
        assign = self._assign_gtids(self._gtid_modes())  # refused before any copy
        if self.snapshot_mode == "lockless":
            (file, position), after = self._snapshot_lockless()
            self._copy_counters()
            self._open_window()  # the copy's changes come again
            self._start_channel(file, position, assign)
            self._await_channel()
            self._strict_after(*after)
        else:
            file, position = self._snapshot()
            self._copy_counters()
            self._start_channel(file, position, assign)
            self._await_channel()
        with self._session("target") as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {name(self.target_db)}.{name(MARKS)} "
                        "(table_name VARCHAR(64) PRIMARY KEY, copied_at DATETIME(6) NOT NULL)")
            for table, _ in self.tables:
                cur.execute(f"INSERT INTO {name(self.target_db)}.{name(MARKS)} VALUES "
                            "(%s, UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE "
                            "copied_at=VALUES(copied_at)", (table,))

    def _check_start_sync(self):
        status = self._status()
        holds, detail = super()._check_start_sync()
        running = status is not None and status.get("Replica_IO_Running") == "Yes" and \
            status.get("Replica_SQL_Running") == "Yes"
        tls = status is not None and status.get("Source_SSL_Allowed") == "Yes" and \
            status.get("Source_SSL_Verify_Server_Cert") == "Yes"
        running = running and (tls or not self.replication_tls)
        open_, mode, workers = self._window() if self.snapshot_mode == "lockless" else (
            False, None, None)
        modes = self._gtid_modes()
        try:
            needed = self._assign_gtids(modes)
        except ContractError:
            needed = None  # no channel setting makes these two compatible
        assigned = self._assigned() if status is not None else None
        given = assigned is not None and assigned["type"] == "UUID" and \
            assigned["value"] == self.assigned_uuid
        gtids_ok = needed is not None and (given if needed else
                                          assigned is None or assigned["type"] == "OFF")
        return holds and running and not open_ and gtids_ok, {  # past the copy's changes
            **detail, "channel": self.channel, "running": running, "tls_verified": tls,
            "snapshot": self.snapshot_mode, "replay_window_open": open_, "exec_mode": mode,
            "applier_threads": workers, "gtid_modes": modes, "gtids_assigned": assigned,
            "gtids_compatible": gtids_ok,
                                   "errors": None if status is None else {
                                       "io": status.get("Last_IO_Error") or None,
                                       "sql": status.get("Last_SQL_Error") or None}}

    def _apply_final_changes(self):
        if self._status() is None:  # the sync never ran here: copy everything instead
            self._copy_content()
            return
        with self._session("source") as cur:
            cur.execute("SHOW BINARY LOG STATUS")
            file, position = cur.fetchone()[:2]
        with self._session("target") as cur:
            cur.execute("SELECT SOURCE_POS_WAIT(%s, %s, %s, %s)",
                        (file, int(position), self.catch_up_seconds, self.channel))
            reached = cur.fetchone()[0]
        if reached is None or reached < 0:
            raise self._behind("the source's position", file, position)
        self._drop_channel()
        if self.snapshot_mode == "lockless":
            self._close_window()  # if the sync's own close never ran
        self._copy_counters()

    def _check_apply_final_changes(self):
        status = self._status()
        equal, detail = self._check_copy_content()
        open_ = self._window()[0] if self.snapshot_mode == "lockless" else False
        return status is None and equal and not open_, {
            **detail, "channel_removed": status is None, "replay_window_open": open_}

    def release_replication(self):
        """Abort: remove the channel; the source no longer keeps logs for it."""
        self._drop_channel()
        if self.snapshot_mode == "lockless":
            self._close_window()  # an abort inside the copy's window

    def replication_released(self):
        return self._status() is None
