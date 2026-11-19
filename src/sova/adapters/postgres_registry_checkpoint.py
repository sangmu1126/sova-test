"""A transaction barrier for the native registry feed, not cloud monitoring.

The migration makes registry/trust history and its change event atomic.
Per-tenant transaction locks exclude new publishers until the watermark is
committed. A busy publisher makes collection fail closed. A baseline older than trigger installation is
refused; no completeness is inferred for the history before installation.
No source, target, edge or general control-plane watermark is produced here.
"""

from contextlib import contextmanager

from sova.adapters.postgres_change_feed import PostgresChangeFeed
from sova.domain.contracts import ContractError, utc
from sova.domain.registry_attestation import REGISTRY_ENVIRONMENT
from sova.ports.plans import RepositoryConflict
from sova_security.adapters.postgres_session import ScopedPostgresSession

MIGRATION = "postgres_registry_change_bindings_v1.sql"
COLLECTOR = "registry-transaction-outbox-v1"


class PostgresRegistryCheckpoint:
    def __init__(self, connect, *, tenant_id):
        self.tenant_id = tenant_id
        self.session = ScopedPostgresSession(connect, tenant_id=tenant_id)

    def checkpoint(self, *, baseline_observed_at):
        baseline = utc(baseline_observed_at)
        with self.session.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout='1000ms'")
            conn.execute("SET LOCAL statement_timeout='5000ms'")
            installed = conn.execute("SELECT applied_at FROM sova_control.schema_migrations WHERE name=%s", (MIGRATION,)).fetchone()
            if installed is None or baseline < utc(installed[0]):
                raise ContractError("Registry baseline predates atomic change collection")
            acquired = conn.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                ("sova.registry.feed.v1/" + self.tenant_id,)).fetchone()[0]
            if not acquired:
                raise RepositoryConflict("Registry publication is in progress; completeness not advanced")
            through = utc(conn.execute("SELECT clock_timestamp()").fetchone()[0])
            if baseline > through:
                raise ContractError("Registry baseline is ahead of the database clock")

            @contextmanager
            def borrowed():
                yield conn

            watermark = PostgresChangeFeed(borrowed, tenant_id=self.tenant_id).advance(
                REGISTRY_ENVIRONMENT, collector_id=COLLECTOR, observed_through=through)
            return {"environment_id": watermark.environment_id,
                    "observed_through": watermark.observed_through.isoformat(),
                    "baseline_observed_at": baseline.isoformat(), "atomic_collection_since": installed[0].isoformat(),
                    "collector_id": COLLECTOR, "coverage": "native registry heads/history and verifier trust history only",
                    "external_environment_coverage": False}
