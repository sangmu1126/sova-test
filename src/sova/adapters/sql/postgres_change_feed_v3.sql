-- Apply after postgres_change_feed_v2.sql as the migration owner.
-- Adds the 'vulnerability' change kind (a newly disclosed weakness; nothing
-- was changed). Replaces the v1 kind CHECK by its generated name; the
-- artifact-digest CHECK, which also mentions kind, is left untouched.
ALTER TABLE sova_control.change_events DROP CONSTRAINT IF EXISTS change_events_kind_check;
ALTER TABLE sova_control.change_events DROP CONSTRAINT IF EXISTS change_events_kind_v3;
ALTER TABLE sova_control.change_events ADD CONSTRAINT change_events_kind_v3 CHECK (kind IN (
    'resource', 'schema', 'policy', 'identity', 'code', 'vulnerability', 'verifier',
    'adapter', 'contract', 'manifest', 'qualification', 'environment_profile',
    'unclassified'));
