-- Migration 020: Add scope to definitions and tool-level MCP grants
-- Scope allows distinguishing built-in platform definitions from instance-specific ones
-- Tool grants enable fine-grained control over which MCP tools an agent can use

-- Add scope column to all definition tables
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'instance';
ALTER TABLE skill_definitions ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'instance';
ALTER TABLE mcp_server_configs ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'instance';

-- Add allowed_tools to agent_mcp_servers junction table
-- NULL means all tools allowed; a JSON array means only those tools
ALTER TABLE agent_mcp_servers ADD COLUMN IF NOT EXISTS allowed_tools JSONB DEFAULT NULL;

-- Comment on the scope values
COMMENT ON COLUMN agent_definitions.scope IS 'built-in = shipped with platform, instance = created by daemon/users';
COMMENT ON COLUMN skill_definitions.scope IS 'built-in = shipped with platform, instance = created by daemon/users';
COMMENT ON COLUMN mcp_server_configs.scope IS 'built-in = shipped with platform, instance = created by daemon/users';
COMMENT ON COLUMN agent_mcp_servers.allowed_tools IS 'JSON array of tool names agent can use. NULL = all tools allowed.';
