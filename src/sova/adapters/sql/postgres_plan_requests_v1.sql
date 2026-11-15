-- Apply after lifecycle v1/v2 as the migration owner, never as the runtime role.
-- What a plan was compiled from: the registered subjects (not their revisions),
-- the requested members and the row bound. Admission recompiles from the same
-- request against the subjects' current revisions; a plan whose inputs moved
-- since then no longer recompiles to itself and is refused.
-- Insert-only: the runtime role needs SELECT and INSERT.
CREATE TABLE IF NOT EXISTS sova_control.plan_requests (
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    plan_id text NOT NULL,
    compiled_digest text NOT NULL CHECK (compiled_digest ~ '^[0-9a-f]{64}$'),
    request text NOT NULL CHECK (octet_length(request) BETWEEN 2 AND 16384),
    requested_by text NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 256),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, application_id, plan_id)
);
ALTER TABLE sova_control.plan_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.plan_requests FORCE ROW LEVEL SECURITY;
DO $migration$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='sova_control'
                 AND tablename='plan_requests' AND policyname='tenant_scope') THEN
    CREATE POLICY tenant_scope ON sova_control.plan_requests
      USING (tenant_id = current_setting('sova.tenant_id', true))
      WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
  END IF;
END
$migration$;
REVOKE ALL ON sova_control.plan_requests FROM PUBLIC;
