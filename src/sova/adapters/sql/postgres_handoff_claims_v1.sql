-- Apply after lifecycle v1/v2 as the migration owner, never as the runtime role.
-- Cross-plan handoff claims (MIG-SPEC-001 §12.5). A claim row binds one
-- absolute resource key and mode to an approved, stored lifecycle plan.
-- The runtime role needs SELECT, INSERT and UPDATE(released_at,released_by)
-- here, plus UPDATE(lock_guard) on approvals to lock the approval row while
-- claiming. No privileges are granted by this migration.
CREATE TABLE IF NOT EXISTS sova_control.handoff_claims (
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    plan_id text NOT NULL,
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    compiled_digest text NOT NULL CHECK (compiled_digest ~ '^[0-9a-f]{64}$'),
    approval_id text NOT NULL,
    claim_key text NOT NULL CHECK (length(claim_key) BETWEEN 1 AND 2048),
    mode text NOT NULL CHECK (mode IN ('read', 'write')),
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    released_at timestamptz,
    released_by text,
    -- A plan is admitted at most once; a later attempt needs a new plan.
    PRIMARY KEY (tenant_id, plan_digest, claim_key),
    FOREIGN KEY (tenant_id, application_id, plan_id, plan_digest)
      REFERENCES sova_control.plans(tenant_id, application_id, plan_id, digest),
    CHECK ((released_at IS NULL) = (released_by IS NULL))
);
CREATE INDEX IF NOT EXISTS handoff_claims_active
    ON sova_control.handoff_claims (tenant_id, claim_key) WHERE released_at IS NULL;
ALTER TABLE sova_control.handoff_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.handoff_claims FORCE ROW LEVEL SECURITY;
DO $migration$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                 AND tablename='handoff_claims' AND policyname='tenant_scope') THEN
    CREATE POLICY tenant_scope ON sova_control.handoff_claims
      USING (tenant_id = current_setting('sova.tenant_id', true))
      WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
  END IF;
END
$migration$;
REVOKE ALL ON sova_control.handoff_claims FROM PUBLIC;
