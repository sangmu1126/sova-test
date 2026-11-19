-- Per-tenant transaction barrier. A runtime role needs no table-wide LOCK or
-- UPDATE privilege on immutable history. Every publisher takes the same
-- transaction-scoped advisory lock before it can publish current state.
CREATE OR REPLACE FUNCTION sova_control.registry_publication_barrier_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, sova_control AS $function$
BEGIN
  IF current_setting('sova.tenant_id',true) IS DISTINCT FROM NEW.tenant_id THEN
    RAISE EXCEPTION 'registry publication tenant mismatch' USING ERRCODE='42501';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('sova.registry.feed.v1/' || NEW.tenant_id,0));
  -- Defaults may have been evaluated before a contended lock. Stamp history
  -- only after the barrier, so it cannot arrive behind an issued watermark.
  IF TG_TABLE_NAME='registrations' THEN
    NEW.registered_at := clock_timestamp();
  ELSIF TG_TABLE_NAME='verifier_trust_events' THEN
    NEW.recorded_at := clock_timestamp();
  ELSE
    NEW.updated_at := clock_timestamp();
  END IF;
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION sova_control.registry_publication_barrier_v1() FROM PUBLIC;
CREATE TRIGGER registry_publication_barrier_v1 BEFORE INSERT OR UPDATE ON sova_control.registration_heads
  FOR EACH ROW EXECUTE FUNCTION sova_control.registry_publication_barrier_v1();
CREATE TRIGGER registry_history_barrier_v1 BEFORE INSERT ON sova_control.registrations
  FOR EACH ROW EXECUTE FUNCTION sova_control.registry_publication_barrier_v1();
CREATE TRIGGER verifier_history_barrier_v1 BEFORE INSERT ON sova_control.verifier_trust_events
  FOR EACH ROW EXECUTE FUNCTION sova_control.registry_publication_barrier_v1();
