-- Apply after lifecycle v1/v2 as the migration owner, never as the runtime role.
-- Registry documents (contracts, dependency snapshots, environment profiles,
-- capability manifests, strategy qualifications) with revisions.
--   registration_heads: the current revision of each subject; a new revision
--     is accepted only from the revision the writer read (compare-and-set).
--   registrations: every revision ever accepted; insert-only history.
-- The runtime role gets SELECT, INSERT on both and UPDATE(revision, digest,
-- updated_at) on the heads only (see sova.service.schema).
CREATE TABLE IF NOT EXISTS sova_control.registration_heads (
    tenant_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('service_contract', 'dependency_snapshot',
        'environment_profile', 'capability_manifest', 'strategy_qualification')),
    subject_id text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 256),
    revision bigint NOT NULL CHECK (revision >= 1),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, kind, subject_id)
);
CREATE TABLE IF NOT EXISTS sova_control.registrations (
    tenant_id text NOT NULL,
    kind text NOT NULL,
    subject_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    canonical text NOT NULL CHECK (octet_length(canonical) BETWEEN 2 AND 1048576),
    registered_by text NOT NULL CHECK (length(registered_by) BETWEEN 1 AND 256),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, kind, subject_id, revision),
    FOREIGN KEY (tenant_id, kind, subject_id)
        REFERENCES sova_control.registration_heads (tenant_id, kind, subject_id)
);
ALTER TABLE sova_control.registration_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.registration_heads FORCE ROW LEVEL SECURITY;
ALTER TABLE sova_control.registrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.registrations FORCE ROW LEVEL SECURITY;
DO $migration$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                 AND tablename='registration_heads' AND policyname='tenant_scope') THEN
    CREATE POLICY tenant_scope ON sova_control.registration_heads
      USING (tenant_id = current_setting('sova.tenant_id', true))
      WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                 AND tablename='registrations' AND policyname='tenant_scope') THEN
    CREATE POLICY tenant_scope ON sova_control.registrations
      USING (tenant_id = current_setting('sova.tenant_id', true))
      WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
  END IF;
END
$migration$;
REVOKE ALL ON sova_control.registration_heads FROM PUBLIC;
REVOKE ALL ON sova_control.registrations FROM PUBLIC;
