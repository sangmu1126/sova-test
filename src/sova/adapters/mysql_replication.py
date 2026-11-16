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
"""

import hashlib
import json
import re

from sova.adapters.mysql_offline_copy import MARKS, MySqlOfflineCopyAdapter, account, name
from sova.domain.contracts import ContractError

_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")


class MySqlReplicationAdapter(MySqlOfflineCopyAdapter):
    def __init__(self, adapter_id, *, source_endpoint, replication_account,
                 replication_password, replication_tls=False, replication_ca_path=None,
                 catch_up_seconds=120, **common):
        super().__init__(adapter_id, **common)
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

    def _start_channel(self, file, position):
        user, _ = account(self.replication_account)
        host, port = self.source_endpoint
        tls = (", SOURCE_SSL=1, SOURCE_SSL_VERIFY_SERVER_CERT=1, SOURCE_SSL_CA=%s"
               if self.replication_tls else "")
        with self._session("target") as cur:
            cur.execute(
                "CHANGE REPLICATION SOURCE TO SOURCE_HOST=%s, SOURCE_PORT=%s, SOURCE_USER=%s, "
                "SOURCE_PASSWORD=%s, SOURCE_LOG_FILE=%s, SOURCE_LOG_POS=%s, "
                f"SOURCE_AUTO_POSITION=0, GET_SOURCE_PUBLIC_KEY=1{tls} FOR CHANNEL %s",
                (host, port, user, self.replication_password, file, position,
                 *((self.replication_ca_path,) if self.replication_tls else ()), self.channel))
            cur.execute(
                f"CHANGE REPLICATION FILTER REPLICATE_REWRITE_DB = (({name(self.source_db)}, "
                f"{name(self.target_db)})), REPLICATE_DO_DB = ({name(self.target_db)}) "
                "FOR CHANNEL %s", (self.channel,))
            cur.execute("START REPLICA FOR CHANNEL %s", (self.channel,))

    # -- actions ---------------------------------------------------------------
    def _start_sync(self):
        self._drop_channel()  # a second run starts over from a new snapshot
        file, position = self._snapshot()
        self._copy_counters()
        self._start_channel(file, position)
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
        return holds and running, {**detail, "channel": self.channel, "running": running,
                                   "tls_verified": tls,
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
            raise ContractError("The replica did not reach the source's position in time")
        self._drop_channel()
        self._copy_counters()

    def _check_apply_final_changes(self):
        status = self._status()
        equal, detail = self._check_copy_content()
        return status is None and equal, {**detail, "channel_removed": status is None}

    def release_replication(self):
        """Abort: remove the channel; the source no longer keeps logs for it."""
        self._drop_channel()

    def replication_released(self):
        return self._status() is None
