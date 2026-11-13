-- Apply after lifecycle v1/v2 as the migration owner, never as the runtime role.
-- Frozen compiled plans (MIG-SPEC-001 §13.3), content-addressed by digest.
-- Admission stores the exact plan it admitted so execution after a restart
-- runs that plan, not a recompilation from possibly changed inputs.
-- Insert-only: the runtime role needs SELECT and INSERT. No privileges here.
CREATE TABLE IF NOT EXISTS sova_control.compiled_plans (
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    compiled_digest text NOT NULL CHECK (compiled_digest ~ '^[0-9a-f]{64}$'),
    canonical text NOT NULL CHECK (length(canonical) BETWEEN 2 AND 1048576),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, compiled_digest)
);
ALTER TABLE sova_control.compiled_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.compiled_plans FORCE ROW LEVEL SECURITY;
DO $migration$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                 AND tablename='compiled_plans' AND policyname='tenant_scope') THEN
    CREATE POLICY tenant_scope ON sova_control.compiled_plans
      USING (tenant_id = current_setting('sova.tenant_id', true))
      WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
  END IF;
END
$migration$;
REVOKE ALL ON sova_control.compiled_plans FROM PUBLIC;
