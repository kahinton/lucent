-- Migration 026: Add force_password_change flag to users table
-- Allows admins to force users to change their password on next login

ALTER TABLE users ADD COLUMN IF NOT EXISTS force_password_change BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN users.force_password_change IS 'When true, user must change password on next login';
