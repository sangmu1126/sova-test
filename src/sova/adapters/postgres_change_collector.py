"""Partial PostgreSQL catalog observations; not complete change capture.

Polling can miss a change followed by a restoration between observations.
The queried tables/ACLs also exclude role, code, automation and host changes.
Observed differences are useful invalidations, but this collector never
advances an environment watermark. A separately qualified, authenticated
coverage barrier is required before admission. Timing alone cannot prove
that a detected change was caused by the plan; callers must establish that
correlation independently before supplying cause.
"""

import hashlib

from sova.domain.change_impact import ChangeEvent, ChangeKind
from sova.domain.contracts import ContractError, canonical_json, identifier

_SCHEMA = """SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position"""
_POLICY = """SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants WHERE table_schema = 'public'
ORDER BY grantee, table_name, privilege_type"""
_DATABASE_ACL = "SELECT coalesce(datacl::text, '') FROM pg_database WHERE datname = %s"


def _fingerprint(rows):
    return hashlib.sha256(canonical_json({"rows": [list(r) for r in rows]},
                                         limit=4_194_304).encode()).hexdigest()


class PostgresCatalogCollector:
    def __init__(self, feed, *, connect, tenant_id, application_id, collector_id, databases):
        if not callable(connect) or not all(
            callable(getattr(feed, n, None)) for n in ("record",)
        ):
            raise ContractError("Change feed and administrative connect required")
        if type(databases) is not dict or not databases:
            raise ContractError("Databases mapped to (environment, resource UID) required")
        self.feed, self.connect = feed, connect
        self.tenant_id = identifier(tenant_id, "tenant_id")
        self.application_id = identifier(application_id)
        self.collector_id = identifier(collector_id, "collector_id")
        self.databases = dict(databases)
        self.last = {}

    def poll(self, *, cause=None):
        """Record catalog changes since the previous poll; returns the events."""
        recorded = []
        for database, (environment, uid) in sorted(self.databases.items()):
            with self.connect(database) as conn:
                at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                schema = _fingerprint(conn.execute(_SCHEMA).fetchall())
                policy = _fingerprint(conn.execute(_POLICY).fetchall()
                                      + conn.execute(_DATABASE_ACL, (database,)).fetchall())
            previous = self.last.get(database)
            if previous is not None:
                for kind, before, after in ((ChangeKind.SCHEMA, previous[0], schema),
                                            (ChangeKind.POLICY, previous[1], policy)):
                    if before != after:
                        event = ChangeEvent(
                            f"{self.collector_id}:{database}:{kind}:{after[:16]}:"
                            f"{at.isoformat()}", self.tenant_id, self.application_id, kind,
                            environment, uid, None, at, cause)
                        self.feed.record(event, collector_id=self.collector_id)
                        recorded.append(event)
            self.last[database] = (schema, policy)
        return tuple(recorded)
