-- Apply after postgres_change_feed_v1.sql as the migration owner.
-- Structural reaffirmations of a compiled plan against a newer authenticated
-- snapshot (INV-08 re-evaluation). Append-only; the latest observation wins.
-- Coordinator role: SELECT and INSERT. No privileges are granted here.
CREATE TABLE IF NOT EXISTS sova_control.plan_reaffirmations (
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    compiled_digest text NOT NULL CHECK (compiled_digest ~ '^[0-9a-f]{64}$'),
    snapshot_digest text NOT NULL CHECK (snapshot_digest ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    actor_id text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, plan_digest, snapshot_digest)
);
ALTER TABLE sova_control.plan_reaffirmations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.plan_reaffirmations FORCE ROW LEVEL SECURITY;
DO $migration$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                 AND tablename='plan_reaffirmations' AND policyname='tenant_scope') THEN
    CREATE POLICY tenant_scope ON sova_control.plan_reaffirmations
      USING (tenant_id = current_setting('sova.tenant_id', true))
      WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
  END IF;
END
$migration$;
REVOKE ALL ON sova_control.plan_reaffirmations FROM PUBLIC;
