"""What each probe observed, under the digest its signed evidence names.

A worker's signed evidence record carries the SHA-256 of the observation it
judged (``artifact_digest``), not the observation. The observation itself is
kept here, as the very canonical JSON the digest was taken of: anyone reading
it can recompute the digest and compare it with the signed record, and the
database refuses a row whose digest does not match its text. Rows are
insert-only and tenant-scoped by row security.
"""

import hashlib
import json

from sova.adapters.postgres_plans import PostgresLifecycleRepository
from sova.domain.contracts import ContractError, canonical_json, identifier


def observation_text(observation):
    return canonical_json(observation, limit=65536)


class PostgresEvidenceObservations:
    def __init__(self, connect, *, tenant_id):
        self.tenant_id = identifier(tenant_id, "tenant_id")
        self.lifecycle = PostgresLifecycleRepository(connect, tenant_id=tenant_id)

    def record(self, application_id, obligation_id, observation):
        """Keep ``observation``; returns its digest (the same one a retry gets)."""
        text = observation_text(observation)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.lifecycle.transaction() as conn:
            conn.execute(
                """INSERT INTO sova_control.evidence_observations
                (tenant_id,application_id,artifact_digest,obligation_id,document)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (self.tenant_id, identifier(application_id, "application_id"), digest,
                 obligation_id, text))
        return digest

    def get(self, application_id, digest):
        if type(digest) is not str or len(digest) != 64 or set(digest) - set("0123456789abcdef"):
            raise ContractError("An observation digest required")
        with self.lifecycle.transaction() as conn:
            row = conn.execute(
                """SELECT obligation_id, document, recorded_at
                FROM sova_control.evidence_observations
                WHERE tenant_id=%s AND application_id=%s AND artifact_digest=%s""",
                (self.tenant_id, identifier(application_id, "application_id"), digest)
            ).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[1].encode()).hexdigest() != digest:
            raise ContractError("Stored observation does not match its digest")
        return {"obligation_id": row[0], "artifact_digest": digest,
                "observation": json.loads(row[1]), "recorded_at": row[2].isoformat()}
