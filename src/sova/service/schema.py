"""The control-plane schema: ordered migrations and the runtime role's grants.

``apply`` runs as the migration owner (never as the runtime role). Each
migration is applied once, in order, inside its own transaction, and recorded
with the SHA-256 of its text; a recorded migration whose text has changed
stops the run, since the database no longer matches what the code expects.
Concurrent runners (two deploy jobs) serialize on an advisory lock, so a
second runner finds the work done.

``grant_runtime`` gives the runtime role exactly what the adapters need:
SELECT and INSERT everywhere, and UPDATE only on the columns that move (heads,
leases, states). Nothing can delete, and history tables cannot be rewritten.
The role itself is created by the operator (it owns no object and bypasses no
row security; the adapters refuse to run otherwise).
"""

from hashlib import sha256
from importlib.resources import files

from psycopg import sql

from sova.adapters.sql_resources import migration_sql
from sova.domain.contracts import ContractError

LOCK = 0x50FA_0001
MIGRATIONS = (
    "postgres_lifecycle_v1.sql",
    "postgres_lifecycle_v2.sql",
    "postgres_handoff_claims_v1.sql",
    "postgres_handoff_authority_v1.sql",
    "postgres_change_feed_v1.sql",
    "postgres_change_feed_v2.sql",
    "postgres_change_feed_v3.sql",
    "postgres_compiled_plans_v1.sql",
    "postgres_operations_v1.sql",
    "postgres_monitoring_v1.sql",
    "postgres_execution_v1.sql",
    "postgres_governance_v1.sql",
    "postgres_governance_v2.sql",
    "postgres_security_observations_v1.sql",
    "postgres_security_response_v1.sql",
    "postgres_security_response_v2.sql",
    "postgres_signed_evidence_v1.sql",
    "postgres_protection_parity_v1.sql",
    "postgres_verifier_registry_v1.sql",
    "postgres_modernization_v1.sql",
    "postgres_migration_programs_v1.sql",
    "postgres_registrations_v1.sql",
    "postgres_plan_requests_v1.sql",
    "postgres_evidence_observations_v1.sql",
    "postgres_verifier_requests_v1.sql",
    "postgres_registry_change_outbox_v1.sql",
    "postgres_registry_change_barrier_v1.sql",
    "postgres_registry_change_bindings_v1.sql",
)
# (table, columns) the runtime role may UPDATE; None means the whole row.
RUNTIME_UPDATES = (
    ("approvals", ("revision", "revoked_at")),
    ("handoff_claims", ("released_at", "released_by")),
    ("handoff_authority", ("plan_digest", "compiled_digest", "revision", "state",
                           "document", "updated_at")),
    ("change_feed_watermarks", ("observed_through", "updated_at")),
    ("monitor_jobs", None),
    ("monitor_runs", None),
    ("monitor_alerts", None),
    ("operations", ("status", "revision", "updated_at")),
    ("operation_outbox", ("owner_id", "lease_token", "lease_until", "attempt", "published_at",
                          "suppressed_at", "publisher_id", "message_id")),
    ("app_execution", ("operation_id", "owner_id", "token", "epoch", "valid_until", "mode",
                       "proof_digest", "proof_valid_until", "recovery_effects")),
    ("effects", ("status", "revision", "observation_document")),
    ("registration_heads", ("revision", "digest", "updated_at")),
)


def text_of(name):
    if name == "postgres_lifecycle_v1.sql":
        return files("sova.adapters").joinpath("sql", name).read_text(encoding="utf-8")
    return migration_sql(name)


def apply(conn):
    """Apply every pending migration; returns the names applied now."""
    conn.autocommit = False
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
        conn.execute("CREATE SCHEMA IF NOT EXISTS sova_control")
        conn.execute("""CREATE TABLE IF NOT EXISTS sova_control.schema_migrations (
            name text PRIMARY KEY, sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
        conn.execute("REVOKE ALL ON sova_control.schema_migrations FROM PUBLIC")
        done = dict(conn.execute("SELECT name, sha256 FROM sova_control.schema_migrations"))
        applied = []
        for name in MIGRATIONS:
            text = text_of(name)
            checksum = sha256(text.encode()).hexdigest()
            if name in done:
                if done[name] != checksum:
                    raise ContractError(f"Applied migration {name} has changed since")
                continue
            conn.execute(text)
            conn.execute("INSERT INTO sova_control.schema_migrations(name, sha256) "
                         "VALUES (%s, %s)", (name, checksum))
            applied.append(name)
        unknown = sorted(set(done) - set(MIGRATIONS))
        if unknown:
            raise ContractError("The database has migrations this code does not know: "
                                + ", ".join(unknown))
    return applied


def grant_runtime(conn, role):
    """Grant the runtime role its exact privileges (idempotent)."""
    who = sql.Identifier(role)
    with conn.transaction():
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA sova_control TO {}").format(who))
        conn.execute(sql.SQL(
            "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA sova_control TO {}").format(who))
        conn.execute(sql.SQL(
            "REVOKE INSERT ON sova_control.schema_migrations FROM {}").format(who))
        conn.execute(sql.SQL(
            "GRANT USAGE ON ALL SEQUENCES IN SCHEMA sova_control TO {}").format(who))
        for table, columns in RUNTIME_UPDATES:
            target = sql.SQL("sova_control.{}").format(sql.Identifier(table))
            if columns is None:
                conn.execute(sql.SQL("GRANT UPDATE ON {} TO {}").format(target, who))
            else:
                conn.execute(sql.SQL("GRANT UPDATE ({}) ON {} TO {}").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c in columns), target, who))
