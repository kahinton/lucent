-- Migration 107: Add the instance-level hyperadmin role and organization suspension state.
-- Existing organizations remain active; suspension is reversible and retains tenant data.

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('member', 'daemon', 'admin', 'owner', 'hyperadmin'));

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;

ALTER TABLE organizations DROP CONSTRAINT IF EXISTS organizations_status_check;
ALTER TABLE organizations ADD CONSTRAINT organizations_status_check
    CHECK (status IN ('active', 'suspended'));

CREATE INDEX IF NOT EXISTS idx_organizations_status ON organizations (status);

COMMENT ON COLUMN users.role IS
    'User role: member, daemon, admin, owner, or instance-level hyperadmin.';
COMMENT ON COLUMN organizations.status IS
    'Organization lifecycle status; suspended organizations retain their data.';
COMMENT ON COLUMN organizations.suspended_at IS
    'Time the organization was suspended; NULL while active.';