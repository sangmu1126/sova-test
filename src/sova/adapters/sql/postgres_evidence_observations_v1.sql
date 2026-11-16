-- Apply after lifecycle v1/v2 as the migration owner, never as the runtime role.
-- What each probe observed, kept with the signed evidence that names it: a
-- signed record carries only the observation's SHA-256, so without the
-- observation itself nobody can see what was measured. The text stored is the
-- canonical JSON the digest was taken of, and the database refuses a row whose
-- digest does not match it.
-- Insert-only: the runtime role needs SELECT and INSERT.
CREATE TABLE IF NOT EXISTS sova_control.evidence_observations (
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    artifact_digest text NOT NULL CHECK (artifact_digest ~ '^[0-9a-f]{64}$'),
    obligation_id text NOT NULL CHECK (length(obligation_id) BETWEEN 1 AND 256),
    document text NOT NULL CHECK (octet_length(document) BETWEEN 2 AND 65536),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, application_id, artifact_digest),
    CONSTRAINT digest_of_document CHECK (
        artifact_digest = encode(sha256(convert_to(document, 'UTF8')), 'hex'))
);
ALTER TABLE sova_control.evidence_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.evidence_observations FORCE ROW LEVEL SECURITY;
DO $migration$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                 AND tablename='evidence_observations' AND policyname='tenant_scope') THEN
    CREATE POLICY tenant_scope ON sova_control.evidence_observations
      USING (tenant_id = current_setting('sova.tenant_id', true))
      WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
  END IF;
END
$migration$;
REVOKE ALL ON sova_control.evidence_observations FROM PUBLIC;
