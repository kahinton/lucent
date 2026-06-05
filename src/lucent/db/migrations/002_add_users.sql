-- Migration: Add users table for external authentication
-- This migration adds proper user management with OAuth/SAML support

-- Create enum for auth providers
-- Using TEXT with CHECK for flexibility to add new providers without migration
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- External identity
    external_id TEXT NOT NULL,  -- The ID from the auth provider
    provider TEXT NOT NULL CHECK (provider IN ('google', 'github', 'saml', 'local')),
    
    -- User profile (synced from provider or manually set)
    email TEXT,
    display_name TEXT,
    avatar_url TEXT,
    
    -- Provider-specific metadata (e.g., SAML attributes, OAuth scopes)
    provider_metadata JSONB DEFAULT '{}',
    
    -- Account status
    is_active BOOLEAN DEFAULT true,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login_at TIMESTAMP WITH TIME ZONE,
    
    -- Ensure unique identity per provider
    CONSTRAINT users_provider_external_id_unique UNIQUE (provider, external_id)
);

-- Create indexes for users table
CREATE INDEX IF NOT EXISTS idx_users_email ON users (email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_provider ON users (provider);
CREATE INDEX IF NOT EXISTS idx_users_active ON users (id) WHERE is_active = true;

-- Add trigger for updated_at on users
DROP TRIGGER IF EXISTS update_users_updated_at ON users;
CREATE TRIGGER update_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Add user_id column to memories table
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS user_id UUID;

-- Create index for user_id on memories
CREATE INDEX IF NOT EXISTS idx_memories_user_id 
ON memories (user_id) WHERE deleted_at IS NULL;

-- Add foreign key constraint (but allow NULL for backward compatibility during migration)
-- We'll enforce NOT NULL after data migration
ALTER TABLE memories
DROP CONSTRAINT IF EXISTS fk_memories_user_id;

ALTER TABLE memories
ADD CONSTRAINT fk_memories_user_id 
FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;

-- Comments
COMMENT ON TABLE users IS 'Stores user accounts with support for multiple OAuth providers and SAML';
COMMENT ON COLUMN users.external_id IS 'Unique identifier from the authentication provider';
COMMENT ON COLUMN users.provider IS 'Authentication provider: google, github, saml, or local';
COMMENT ON COLUMN users.provider_metadata IS 'Provider-specific data like SAML attributes or OAuth tokens';
COMMENT ON COLUMN memories.user_id IS 'Foreign key to users table; NULL allowed for legacy records';
