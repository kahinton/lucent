-- Migration 086: Runtime settings
--
-- Stores safe, non-secret runtime configuration values in the database so
-- administrators can manage them from the Settings UI. Environment variables
-- remain fallback values when a setting has no database row.

CREATE TABLE IF NOT EXISTS runtime_settings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    key TEXT NOT NULL,
    value JSONB NOT NULL,
    value_type TEXT NOT NULL CHECK (value_type IN ('boolean', 'integer', 'float', 'string', 'json')),

    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    UNIQUE (organization_id, key)
);

CREATE INDEX IF NOT EXISTS idx_runtime_settings_org_key
    ON runtime_settings (organization_id, key);

COMMENT ON TABLE runtime_settings IS
    'Org-scoped, non-secret runtime settings managed from Settings. Missing rows fall back to env vars/defaults.';
COMMENT ON COLUMN runtime_settings.key IS
    'Stable allowlisted setting key from lucent.settings, not an arbitrary environment variable name.';
COMMENT ON COLUMN runtime_settings.value IS
    'Typed JSON value validated by the application before insert/update.';

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
    EXECUTE 'GRANT SELECT ON runtime_settings TO lucent_daemon';
    RAISE NOTICE 'Granted daemon role SELECT on runtime_settings';
  ELSE
    RAISE NOTICE 'lucent_daemon role not found — skipping runtime_settings grant';
  END IF;
END $$;
