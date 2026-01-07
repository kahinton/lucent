-- Migration: Add role-based access control
-- Adds role column to users table for permission management

-- Add role column with default 'member'
ALTER TABLE users 
ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'member';

-- Add check constraint for valid roles
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'users_role_check'
    ) THEN
        ALTER TABLE users 
        ADD CONSTRAINT users_role_check 
        CHECK (role IN ('member', 'admin', 'owner'));
    END IF;
END $$;

-- Ensure exactly one owner per organization
-- This prevents orphaned orgs and ensures clear accountability
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_owner_per_org 
ON users (organization_id) WHERE role = 'owner';

-- Index for finding users by role within an org (useful for admin queries)
CREATE INDEX IF NOT EXISTS idx_users_org_role 
ON users (organization_id, role);

-- Update existing users to have 'member' role if null
UPDATE users SET role = 'member' WHERE role IS NULL;

-- Make role NOT NULL after backfill
ALTER TABLE users ALTER COLUMN role SET NOT NULL;

-- Comments
COMMENT ON COLUMN users.role IS 'User role: member (default), admin, or owner. Controls permissions within organization.';
