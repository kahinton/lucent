-- Migration 105: Cache native MCP tool descriptors for sandboxed managed proxies.

ALTER TABLE managed_tool_definitions
    ADD COLUMN IF NOT EXISTS discovered_tools JSONB DEFAULT NULL;

ALTER TABLE managed_tool_definitions
    ADD COLUMN IF NOT EXISTS tools_discovered_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN managed_tool_definitions.discovered_tools IS
    'Native MCP tool descriptors discovered by warming a managed mcp_proxy runtime.';
COMMENT ON COLUMN managed_tool_definitions.tools_discovered_at IS
    'Timestamp of the most recent managed MCP proxy tool discovery.';