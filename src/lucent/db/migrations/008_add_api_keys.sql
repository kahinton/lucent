-- Migration: Add API keys table for programmatic access
-- Allows users to generate API keys for MCP/API authentication

CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Ownership
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    
    -- Key details
    name TEXT NOT NULL,  -- User-provided name for the key (e.g., "VS Code", "Claude Desktop")
    key_prefix TEXT NOT NULL,  -- First 8 chars of the key for display (e.g., "mcp_abc1...")
    key_hash TEXT NOT NULL,  -- bcrypt hash of the full key
    
    -- Permissions/scopes (for future use)
    scopes TEXT[] DEFAULT ARRAY['read', 'write'],
    
    -- Usage tracking
    last_used_at TIMESTAMP WITH TIME ZONE,
    use_count INTEGER DEFAULT 0,
    
    -- Expiration (NULL = never expires)
    expires_at TIMESTAMP WITH TIME ZONE,
    
    -- Status
    is_active BOOLEAN DEFAULT true,
    revoked_at TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT api_keys_key_prefix_unique UNIQUE (key_prefix)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys (user_id) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_api_keys_org_id ON api_keys (organization_id) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_api_keys_key_prefix ON api_keys (key_prefix);

-- Trigger for updated_at
DROP TRIGGER IF EXISTS update_api_keys_updated_at ON api_keys;
CREATE TRIGGER update_api_keys_updated_at
    BEFORE UPDATE ON api_keys
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Comments
COMMENT ON TABLE api_keys IS 'Stores API keys for programmatic access to the MCP server and REST API';
COMMENT ON COLUMN api_keys.key_prefix IS 'First 8 characters of the key for identification without exposing the full key';
COMMENT ON COLUMN api_keys.key_hash IS 'Bcrypt hash of the full API key for secure verification';
COMMENT ON COLUMN api_keys.scopes IS 'Array of permission scopes granted to this key';
