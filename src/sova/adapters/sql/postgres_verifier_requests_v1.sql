-- Public keys and exact app-scoped grants; append-only requested/approved history.
CREATE TABLE IF NOT EXISTS sova_control.verifier_requests (
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    request_id text NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    PRIMARY KEY (tenant_id, application_id, request_id)
);
CREATE TABLE IF NOT EXISTS sova_control.verifier_request_approvals (
    tenant_id text NOT NULL,
    application_id text NOT NULL,
    request_id text NOT NULL,
    approval_id text NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    PRIMARY KEY (tenant_id, application_id, request_id),
    UNIQUE (tenant_id, application_id, approval_id),
    FOREIGN KEY (tenant_id, application_id, request_id)
        REFERENCES sova_control.verifier_requests (tenant_id, application_id, request_id)
);
ALTER TABLE sova_control.verifier_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.verifier_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE sova_control.verifier_request_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE sova_control.verifier_request_approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON sova_control.verifier_requests
    USING (tenant_id = current_setting('sova.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
CREATE POLICY tenant_scope ON sova_control.verifier_request_approvals
    USING (tenant_id = current_setting('sova.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('sova.tenant_id', true));
REVOKE ALL ON sova_control.verifier_requests FROM PUBLIC;
REVOKE ALL ON sova_control.verifier_request_approvals FROM PUBLIC;
