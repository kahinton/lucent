-- Rollback migration 107. Refuses rollback while any hyperadmin users exist.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM users WHERE role = 'hyperadmin') THEN
        RAISE EXCEPTION 'Cannot remove hyperadmin role while hyperadmin users exist';
    END IF;
END $$;

DROP INDEX IF EXISTS idx_organizations_status;
ALTER TABLE organizations DROP CONSTRAINT IF EXISTS organizations_status_check;
ALTER TABLE organizations DROP COLUMN IF EXISTS suspended_at;
ALTER TABLE organizations DROP COLUMN IF EXISTS status;

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('member', 'daemon', 'admin', 'owner'));