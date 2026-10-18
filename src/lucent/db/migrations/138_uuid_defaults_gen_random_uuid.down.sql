-- Rollback for migration 138: restore uuid_generate_v4() defaults.
-- Requires the uuid-ossp extension (created by migration 001).

ALTER TABLE admin_audit_log   ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE api_keys          ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE memories          ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE memory_access_log ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE memory_audit_log  ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE organizations     ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE runtime_settings  ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE schedule_runs     ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE schedules         ALTER COLUMN id SET DEFAULT uuid_generate_v4();
ALTER TABLE users             ALTER COLUMN id SET DEFAULT uuid_generate_v4();
