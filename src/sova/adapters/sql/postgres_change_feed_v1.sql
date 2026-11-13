-- Apply after lifecycle v1/v2 as the migration owner, never as the runtime role.
-- Change events and collector watermarks for change impact (MIG-SPEC-001
-- INV-08, §23.3). Events are append-only; a watermark is one collector's
-- claim that every change in an environment up to observed_through has been
-- recorded, and it never moves backwards.
-- Collector role: INSERT on change_events, SELECT/INSERT and
-- UPDATE(observed_through,updated_at) on change_feed_watermarks.
-- Coordinator role: SELECT on both. No privileges are granted here.
CREATE TABLE IF NOT EXISTS sova_control.change_events (
    tenant_id text NOT NULL,
    event_id text NOT NULL CHECK (length(event_id) BETWEEN 1 AND 256),
    application_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN (
        'resource', 'schema', 'policy', 'identity', 'code', 'verifier', 'adapter',
        'contract', 'manifest', 'qualification', 'environment_profile', 'unclassified')),
    environment_id text,
    subject text,
    artifact_digest text CHECK (artifact_digest ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    cause_plan_digest text CHECK (cause_plan_digest ~ '^[0-9a-f]{64}$'),
    cause_step_id text,
    collector_id text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id),
    CHECK ((cause_plan_digest IS NULL) = (cause_step_id IS NULL)),
    CHECK (artifact_digest IS NULL OR kind IN
           ('contract', 'manifest', 'qualification', 'environment_profile'))
);
CREATE INDEX IF NOT EXISTS change_events_by_time
    ON sova_control.change_events (tenant_id, observed_at);
CREATE TABLE IF NOT EXISTS sova_control.change_feed_watermarks (
    tenant_id text NOT NULL,
    environment_id text NOT NULL,
    collector_id text NOT NULL,
    observed_through timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, environment_id)
);
DO $migration$
DECLARE
  name text;
BEGIN
  FOREACH name IN ARRAY ARRAY['change_events', 'change_feed_watermarks'] LOOP
    EXECUTE format('ALTER TABLE sova_control.%I ENABLE ROW LEVEL SECURITY', name);
    EXECUTE format('ALTER TABLE sova_control.%I FORCE ROW LEVEL SECURITY', name);
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                   AND tablename=name AND policyname='tenant_scope') THEN
      EXECUTE format($p$CREATE POLICY tenant_scope ON sova_control.%I
        USING (tenant_id = current_setting('sova.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('sova.tenant_id', true))$p$, name);
    END IF;
    EXECUTE format('REVOKE ALL ON sova_control.%I FROM PUBLIC', name);
  END LOOP;
END
$migration$;
