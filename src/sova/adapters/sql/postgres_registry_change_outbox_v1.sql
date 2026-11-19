-- Atomic native registry/trust history -> change events. This covers these
-- transactions only. It never advances any cloud/cluster/global watermark.
-- SECURITY INVOKER and forced tenant RLS remain in force.
CREATE OR REPLACE FUNCTION sova_control.registry_change_outbox_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, sova_control AS $function$
DECLARE
  event_kind text;
  app text;
  data jsonb;
BEGIN
  data := NEW.canonical::jsonb;
  event_kind := CASE NEW.kind
    WHEN 'service_contract' THEN 'contract'
    WHEN 'capability_manifest' THEN 'manifest'
    WHEN 'strategy_qualification' THEN 'qualification'
    WHEN 'environment_profile' THEN 'environment_profile'
    ELSE 'resource' END;
  app := CASE NEW.kind
    WHEN 'service_contract' THEN NEW.subject_id
    WHEN 'strategy_qualification' THEN data->>'service_id'
    WHEN 'dependency_snapshot' THEN data->'graph'->>'application_id'
    ELSE 'registry' END;
  IF app IS NULL OR app = '' THEN
    RAISE EXCEPTION 'registry change lacks application binding';
  END IF;
  INSERT INTO sova_control.change_events
    (tenant_id,event_id,application_id,kind,environment_id,subject,
     artifact_digest,observed_at,collector_id)
  VALUES
    (NEW.tenant_id,'registry-' || NEW.kind || '-' || NEW.revision::text || '-' || NEW.digest,app,event_kind,
     'sova-contract-registry',CASE WHEN NEW.kind='dependency_snapshot' THEN NULL ELSE NEW.subject_id END,
     CASE WHEN NEW.kind='dependency_snapshot' THEN NULL ELSE NEW.digest END,
     NEW.registered_at,'registry-transaction-outbox-v1');
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION sova_control.registry_change_outbox_v1() FROM PUBLIC;
CREATE TRIGGER registry_change_outbox_v1 AFTER INSERT ON sova_control.registrations
  FOR EACH ROW EXECUTE FUNCTION sova_control.registry_change_outbox_v1();

CREATE OR REPLACE FUNCTION sova_control.verifier_change_outbox_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, sova_control AS $function$
BEGIN
  -- A key may authorize several verifier versions. Unknown subject widens
  -- impact instead of dropping a revocation or guessing a single version.
  INSERT INTO sova_control.change_events
    (tenant_id,event_id,application_id,kind,environment_id,subject,
     artifact_digest,observed_at,collector_id)
  VALUES
    (NEW.tenant_id,'verifier-' || NEW.digest,NEW.application_id,'verifier',NULL,NULL,
     NULL,NEW.recorded_at,'registry-transaction-outbox-v1');
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION sova_control.verifier_change_outbox_v1() FROM PUBLIC;
CREATE TRIGGER verifier_change_outbox_v1 AFTER INSERT ON sova_control.verifier_trust_events
  FOR EACH ROW EXECUTE FUNCTION sova_control.verifier_change_outbox_v1();

-- A runtime credential must not rewind a head without appending history. The
-- normal CAS writer creates/moves the head first and appends history in the
-- same transaction, so history binding is checked at commit, not mid-write.
CREATE OR REPLACE FUNCTION sova_control.registry_head_monotonic_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, sova_control AS $function$
BEGIN
  IF NEW.revision <> OLD.revision + 1 THEN
    RAISE EXCEPTION 'registry head revision must advance exactly once';
  END IF;
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION sova_control.registry_head_monotonic_v1() FROM PUBLIC;
CREATE TRIGGER registry_head_monotonic_v1 BEFORE UPDATE ON sova_control.registration_heads
  FOR EACH ROW EXECUTE FUNCTION sova_control.registry_head_monotonic_v1();

CREATE OR REPLACE FUNCTION sova_control.registry_head_history_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, sova_control AS $function$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sova_control.registrations r
    WHERE (r.tenant_id,r.kind,r.subject_id,r.revision,r.digest)=
          (NEW.tenant_id,NEW.kind,NEW.subject_id,NEW.revision,NEW.digest)) THEN
    RAISE EXCEPTION 'registry head lacks matching immutable history';
  END IF;
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION sova_control.registry_head_history_v1() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER registry_head_history_v1 AFTER INSERT OR UPDATE ON sova_control.registration_heads
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  EXECUTE FUNCTION sova_control.registry_head_history_v1();
