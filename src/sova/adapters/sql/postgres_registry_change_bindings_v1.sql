-- Align registry events with ImpactIndex's semantic identities. In particular,
-- registering an observation is not itself a physical-resource mutation.
ALTER TABLE sova_control.change_events DROP CONSTRAINT change_events_kind_v3;
ALTER TABLE sova_control.change_events ADD CONSTRAINT change_events_kind_v4
  CHECK (kind IN ('resource','schema','policy','identity','code','vulnerability',
    'verifier','adapter','contract','manifest','qualification','environment_profile',
    'dependency_snapshot','unclassified'));
ALTER TABLE sova_control.change_events DROP CONSTRAINT change_events_check1;
ALTER TABLE sova_control.change_events ADD CONSTRAINT change_events_registry_digest_v2
  CHECK (artifact_digest IS NULL OR kind IN ('contract','manifest','qualification',
    'environment_profile','dependency_snapshot'));

CREATE OR REPLACE FUNCTION sova_control.registry_change_outbox_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, sova_control AS $function$
DECLARE
  event_kind text;
  app text;
  event_subject text;
  data jsonb;
BEGIN
  data := NEW.canonical::jsonb;
  event_kind := CASE NEW.kind
    WHEN 'service_contract' THEN 'contract'
    WHEN 'capability_manifest' THEN 'manifest'
    WHEN 'strategy_qualification' THEN 'qualification'
    WHEN 'environment_profile' THEN 'environment_profile'
    WHEN 'dependency_snapshot' THEN 'dependency_snapshot'
    ELSE 'unclassified' END;
  app := CASE NEW.kind
    WHEN 'service_contract' THEN NEW.subject_id
    WHEN 'strategy_qualification' THEN data->>'service_id'
    WHEN 'dependency_snapshot' THEN data->'graph'->>'application_id'
    ELSE 'registry' END;
  event_subject := NEW.subject_id;
  IF NEW.kind='capability_manifest' THEN
    event_subject := data->>'strategy_id';
  ELSIF NEW.kind='strategy_qualification' THEN
    SELECT CASE WHEN count(DISTINCT r.canonical::jsonb->>'strategy_id')=1
                THEN max(r.canonical::jsonb->>'strategy_id') ELSE NULL END
      INTO event_subject FROM sova_control.registrations r
      WHERE r.tenant_id=NEW.tenant_id AND r.kind='capability_manifest'
        AND r.digest=data->>'manifest_digest';
  END IF;
  IF app IS NULL OR app='' THEN
    RAISE EXCEPTION 'registry change lacks application binding';
  END IF;
  INSERT INTO sova_control.change_events
    (tenant_id,event_id,application_id,kind,environment_id,subject,
     artifact_digest,observed_at,collector_id)
  VALUES
    (NEW.tenant_id,'registry-' || NEW.kind || '-' || NEW.revision::text || '-' || NEW.digest,
     app,event_kind,'sova-contract-registry',event_subject,NEW.digest,
     NEW.registered_at,'registry-transaction-outbox-v1');
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION sova_control.registry_change_outbox_v1() FROM PUBLIC;
