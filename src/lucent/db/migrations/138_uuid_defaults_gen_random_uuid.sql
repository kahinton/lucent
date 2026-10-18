-- Migration 138: move uuid primary-key defaults off the uuid-ossp extension.
--
-- Migration 001 created uuid-ossp and defaulted every uuid primary key to
-- uuid_generate_v4(). gen_random_uuid() is core in PostgreSQL 13+, so no column
-- default needs uuid-ossp after this migration. The extension is intentionally
-- NOT dropped here: migration 001 still issues CREATE EXTENSION uuid-ossp on a
-- fresh database, and the down migration below needs it. The point is that a
-- deployment can now stop requiring it for the schema's defaults — useful on
-- managed PostgreSQL where CREATE EXTENSION needs an allowlist.
--
-- Reversal (.down) restores the uuid_generate_v4() defaults.

ALTER TABLE admin_audit_log   ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE api_keys          ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE memories          ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE memory_access_log ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE memory_audit_log  ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE organizations     ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE runtime_settings  ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE schedule_runs     ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE schedules         ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE users             ALTER COLUMN id SET DEFAULT gen_random_uuid();
