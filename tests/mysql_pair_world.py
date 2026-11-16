"""The MySQL world across two servers: every administrative reading goes to the
server that holds the side it reads, and the adapter is the change-capture
one (the offline copy template's actions are the same adapter's)."""

import hashlib
import re

from mysql_business import SCHEMA, TABLES
from mysql_pair_business import REPLICATION
from mysql_world import MySqlWorld

from sova.adapters.mysql_replication import MySqlReplicationAdapter


class MySqlPairWorld(MySqlWorld):
    def _adapter(self, source, target):
        return MySqlReplicationAdapter(
            "mysql-replication", connect=self.b["connect"], source_db=self._db(source),
            target_db=self._db(target), app_account=self.b["app"], tables=TABLES,
            schema_sql=SCHEMA, route=self._route, writer_accounts=tuple(self.writers.values()),
            session_wait_seconds=2.0, source_endpoint=self.b["source_endpoint"],
            replication_account=f"{REPLICATION[0]}@%", replication_password=REPLICATION[1],
            catch_up_seconds=60)

    def _on(self, side, query, args=None):
        with self.b["connect"](self._db(side)) as conn, conn.cursor() as cur:
            cur.execute(query, args)
            rows = cur.fetchall()
            conn.commit()
            return rows

    def _admin(self, query, args=None):
        return self._on("source", query, args)

    def content(self, side):
        db = self._db(side)
        if not self._on(side, "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
                        (db,)):
            return None
        h = hashlib.sha256()
        with self.b["connect"](db) as conn, conn.cursor() as cur:
            cur.execute("SET SESSION time_zone='+00:00'")
            for table, _ in TABLES:
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
            grants = self._on(side, f"SHOW GRANTS FOR {self.b['quoted'](writer)}")
            user, host = writer.rsplit("@", 1)
            locked = self._on(side, "SELECT account_locked FROM mysql.user WHERE user=%s AND "
                                    "host=%s", (user, host))[0][0]
            out.append((writer, locked, sorted(g[0] for g in grants if f"`{db}`" in g[0])))
        return out

    def tamper_target(self):
        self._on("target", f"UPDATE `{self._db('target')}`.orders SET amount = amount + 1 "
                           "WHERE id = 1")

    def reopen_source(self):
        app = self.b["quoted"](self.b["app"])
        self._on("source", f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{self._db('source')}`.* "
                           f"TO {app}")
        self._on("source", f"ALTER USER {app} ACCOUNT UNLOCK")

    def channel(self):
        """The unit's replication channel on the target server, if any."""
        with self.b["connect"](self._db("target")) as conn, conn.cursor() as cur:
            cur.execute("SHOW REPLICA STATUS")
            columns = [d[0] for d in cur.description or ()]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        return [r for r in rows if (r.get("Channel_Name") or "").startswith("sova_")]
