-- Migration: Add password hash column for basic auth support
-- This enables username/password authentication alongside API key auth

-- Add password_hash column to users table
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- Update provider CHECK constraint to include 'basic' auth provider
-- First drop the old constraint, then add a new one
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_provider_check;
ALTER TABLE users ADD CONSTRAINT users_provider_check 
    CHECK (provider IN ('google', 'github', 'saml', 'local', 'basic'));

-- Add session_token column for web UI sessions
-- This stores a hashed session token for cookie-based auth
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_expires_at TIMESTAMP WITH TIME ZONE;

-- Index for session token lookups
CREATE INDEX IF NOT EXISTS idx_users_session_token 
ON users (session_token) WHERE session_token IS NOT NULL;

-- Comments
COMMENT ON COLUMN users.password_hash IS 'Bcrypt hashed password for basic auth provider';
COMMENT ON COLUMN users.session_token IS 'Hashed session token for web UI cookie auth';
COMMENT ON COLUMN users.session_expires_at IS 'When the session token expires';
