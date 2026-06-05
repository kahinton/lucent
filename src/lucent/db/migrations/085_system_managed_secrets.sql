-- Migration 085: System-managed secrets
-- Allows Lucent-owned application secrets (for example cookie signing) to be
-- stored through the configured secret provider without assigning them to an
-- individual user or group.

ALTER TABLE secrets
    ADD COLUMN IF NOT EXISTS system_managed BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE secrets
    DROP CONSTRAINT IF EXISTS ck_secret_owner;

ALTER TABLE secrets
    ADD CONSTRAINT ck_secret_owner CHECK (
        (
            system_managed = true
            AND owner_user_id IS NULL
            AND owner_group_id IS NULL
        )
        OR
        (
            system_managed = false
            AND (owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL)
        )
    );

CREATE UNIQUE INDEX IF NOT EXISTS uniq_secrets_system_key_org
    ON secrets(key, organization_id)
    WHERE system_managed = true;

CREATE INDEX IF NOT EXISTS idx_secrets_system_org
    ON secrets(organization_id)
    WHERE system_managed = true;

COMMENT ON COLUMN secrets.system_managed IS
  'True for Lucent-owned application secrets managed by startup code rather than a user or group.';
