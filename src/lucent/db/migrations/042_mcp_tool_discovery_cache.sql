-- Migration: Add tool discovery caching to mcp_server_configs
-- Caches discovered MCP tools so the UI doesn't need to probe on every page load.

ALTER TABLE mcp_server_configs
    ADD COLUMN IF NOT EXISTS discovered_tools JSONB DEFAULT NULL;

ALTER TABLE mcp_server_configs
    ADD COLUMN IF NOT EXISTS tools_discovered_at TIMESTAMPTZ DEFAULT NULL;
