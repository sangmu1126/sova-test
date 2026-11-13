-- Apply after postgres_handoff_claims_v1.sql as the migration owner.
-- Per-unit business authority (MIG-SPEC-001 §15). One current row per unit,
-- changed only by compare-and-set on revision, plus an append-only history.
-- The runtime role needs SELECT and INSERT on both tables and
-- UPDATE(plan_digest,compiled_digest,revision,state,document,updated_at) on
-- handoff_authority. No privileges are granted by this migration.
CREATE TABLE IF NOT EXISTS sova_control.handoff_authority (
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    unit_digest text NOT NULL CHECK (unit_digest ~ '^[0-9a-f]{64}$'),
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    compiled_digest text NOT NULL CHECK (compiled_digest ~ '^[0-9a-f]{64}$'),
    revision bigint NOT NULL CHECK (revision >= 1),
    state text NOT NULL,
    document jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, application_id, unit_digest)
);
CREATE TABLE IF NOT EXISTS sova_control.handoff_authority_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    unit_digest text NOT NULL,
    plan_digest text NOT NULL,
    revision bigint NOT NULL,
    action text NOT NULL,
    actor_id text NOT NULL,
    document jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, application_id, unit_digest, plan_digest, revision)
);
DO $migration$
DECLARE tab text;
BEGIN
  FOREACH tab IN ARRAY ARRAY['handoff_authority','handoff_authority_events'] LOOP
    EXECUTE format('ALTER TABLE sova_control.%I ENABLE ROW LEVEL SECURITY', tab);
    EXECUTE format('ALTER TABLE sova_control.%I FORCE ROW LEVEL SECURITY', tab);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                   AND tablename=tab AND policyname='tenant_scope') THEN
      EXECUTE format('CREATE POLICY tenant_scope ON sova_control.%I
        USING (tenant_id = current_setting(''sova.tenant_id'', true))
        WITH CHECK (tenant_id = current_setting(''sova.tenant_id'', true))', tab);
    END IF;
  END LOOP;
END
$migration$;
REVOKE ALL ON sova_control.handoff_authority FROM PUBLIC;
REVOKE ALL ON sova_control.handoff_authority_events FROM PUBLIC;
