-- Append-only semantic correction: an initial key grant, or a scope-only
-- expansion under exactly the same key/version/validity, is an authorization
-- policy change. It invalidates existing evidence but does not replace the
-- verifier implementation or the plan's input snapshot. All other changes
-- keep the conservative verifier-change classification and block stale plans.
-- Historical events are deliberately not rewritten.
CREATE OR REPLACE FUNCTION sova_control.verifier_change_outbox_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, sova_control AS $function$
DECLARE
  prior_trust jsonb;
  next_trust jsonb;
  grant_only boolean := false;
BEGIN
  next_trust := NEW.document->'trust';
  SELECT e.document->'trust' INTO prior_trust
    FROM sova_control.verifier_trust_events e
    WHERE (e.tenant_id,e.application_id,e.issuer_id,e.key_id)=
          (NEW.tenant_id,NEW.application_id,NEW.issuer_id,NEW.key_id)
      AND e.revision=NEW.revision-1;
  IF NEW.document->'schema_version'='1'::jsonb
     AND NEW.document->>'tenant_id'=NEW.tenant_id
     AND NEW.document->>'application_id'=NEW.application_id
     AND NEW.document->'revision'=to_jsonb(NEW.revision)
     AND jsonb_typeof(next_trust)='object'
     AND next_trust->>'issuer_id'=NEW.issuer_id
     AND next_trust->>'key_id'=NEW.key_id
     AND (next_trust->>'public_key_hex') ~ '^[0-9a-f]{64}$'
     AND next_trust->'revoked'='false'::jsonb
     AND jsonb_typeof(next_trust->'allowed_scopes')='array'
     AND jsonb_array_length(next_trust->'allowed_scopes') BETWEEN 1 AND 256
     AND NOT EXISTS (
       SELECT 1 FROM jsonb_array_elements(next_trust->'allowed_scopes') AS s(scope)
       WHERE jsonb_typeof(scope) IS DISTINCT FROM 'object'
          OR scope->>'tenant_id' IS DISTINCT FROM NEW.tenant_id
          OR scope->>'application_id' IS DISTINCT FROM NEW.application_id
          OR jsonb_typeof(scope->'environment_id') IS DISTINCT FROM 'string'
          OR jsonb_typeof(scope->'resource_id') IS DISTINCT FROM 'string'
     ) THEN
    IF NEW.revision=1 THEN
      grant_only := true;
    ELSIF prior_trust IS NOT NULL THEN
      grant_only := (next_trust - 'allowed_scopes') = (prior_trust - 'allowed_scopes')
        AND jsonb_typeof(prior_trust->'allowed_scopes')='array'
        AND ((next_trust->'allowed_scopes') @> (prior_trust->'allowed_scopes'));
    END IF;
  END IF;
  INSERT INTO sova_control.change_events
    (tenant_id,event_id,application_id,kind,environment_id,subject,
     artifact_digest,observed_at,collector_id)
  VALUES
    (NEW.tenant_id,'verifier-' || NEW.digest,NEW.application_id,
     CASE WHEN grant_only THEN 'policy' ELSE 'verifier' END,NULL,NULL,
     NULL,NEW.recorded_at,
     CASE WHEN grant_only THEN 'registry-trust-grant-outbox-v1'
          ELSE 'registry-transaction-outbox-v1' END);
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION sova_control.verifier_change_outbox_v1() FROM PUBLIC;
