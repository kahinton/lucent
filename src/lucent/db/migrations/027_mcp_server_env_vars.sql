-- Add env_vars column to mcp_server_configs for environment variable support
ALTER TABLE mcp_server_configs ADD COLUMN IF NOT EXISTS env_vars JSONB DEFAULT '{}';
