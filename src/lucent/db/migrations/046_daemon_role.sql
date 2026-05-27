-- Migration 046: Add 'daemon' role for service accounts
-- Provides scoped permissions (memory read/write/delete all) without admin privileges

-- Update the role CHECK constraint to include 'daemon'
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('member', 'daemon', 'admin', 'owner'));

-- Update existing daemon-service user from 'admin' to 'daemon'
UPDATE users SET role = 'daemon' WHERE external_id = 'daemon-service';
