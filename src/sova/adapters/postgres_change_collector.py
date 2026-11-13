"""Catalog change collector for PostgreSQL databases (INV-08, §23.3).

Each poll fingerprints, per database, its table structure (columns and types)
and its access policy (table grants and the database ACL), records a
``schema`` or ``policy`` change event when a fingerprint differs from the
previous poll, and then advances the environment's watermark to the database
clock time of that poll. Events are recorded before the watermark moves, so a
watermark never claims a change it has not written.

``cause`` attributes the changes found by one poll to a plan step. A caller
should pass it only for a poll taken right after that step's own effect; the
impact assessment still treats an attribution outside the step's write scope
as foreign. Production collectors should attribute by correlation IDs carried
in the change itself rather than by timing. Data changes (row writes) are not
catalog changes and are not reported here.
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
            callable(getattr(feed, n, None)) for n in ("record", "advance")
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
            self.feed.advance(environment, collector_id=self.collector_id,
                              observed_through=at)
        return tuple(recorded)
