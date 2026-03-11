-- Migration: Add agent, skill, and MCP server definition tables
-- Supports global (shipped) and instance (learned) definitions
-- with approval workflow for daemon-created definitions.

-- Agent definitions: roles the daemon can fill
CREATE TABLE IF NOT EXISTS agent_definitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(64) NOT NULL,
    description TEXT,
    content TEXT NOT NULL,                    -- The agent.md content
    scope VARCHAR(16) NOT NULL DEFAULT 'instance',  -- 'global' or 'instance'
    status VARCHAR(16) NOT NULL DEFAULT 'proposed', -- 'proposed', 'approved', 'active', 'rejected', 'archived'
    created_by UUID REFERENCES users(id),
    approved_by UUID REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    organization_id UUID REFERENCES organizations(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, organization_id)
);

-- Skill definitions: competencies that support roles
CREATE TABLE IF NOT EXISTS skill_definitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(64) NOT NULL,
    description TEXT,
    content TEXT NOT NULL,                    -- The SKILL.md content
    scope VARCHAR(16) NOT NULL DEFAULT 'instance',
    status VARCHAR(16) NOT NULL DEFAULT 'proposed',
    created_by UUID REFERENCES users(id),
    approved_by UUID REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    organization_id UUID REFERENCES organizations(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, organization_id)
);

-- MCP server configs: tool connections the daemon can use
CREATE TABLE IF NOT EXISTS mcp_server_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(64) NOT NULL,
    description TEXT,
    server_type VARCHAR(16) NOT NULL DEFAULT 'http',  -- 'http', 'stdio'
    url TEXT,                                 -- For http type
    command TEXT,                             -- For stdio type
    args JSONB DEFAULT '[]',                  -- For stdio type
    headers JSONB DEFAULT '{}',               -- Auth headers etc
    status VARCHAR(16) NOT NULL DEFAULT 'proposed',
    created_by UUID REFERENCES users(id),
    approved_by UUID REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    organization_id UUID REFERENCES organizations(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, organization_id)
);

-- Junction: which skills an agent has access to
CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id UUID NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    skill_id UUID NOT NULL REFERENCES skill_definitions(id) ON DELETE CASCADE,
    PRIMARY KEY (agent_id, skill_id)
);

-- Junction: which MCP servers an agent has access to
CREATE TABLE IF NOT EXISTS agent_mcp_servers (
    agent_id UUID NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    mcp_server_id UUID NOT NULL REFERENCES mcp_server_configs(id) ON DELETE CASCADE,
    PRIMARY KEY (agent_id, mcp_server_id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_agent_defs_org_status ON agent_definitions(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_skill_defs_org_status ON skill_definitions(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_mcp_configs_org_status ON mcp_server_configs(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_defs_scope ON agent_definitions(scope);
CREATE INDEX IF NOT EXISTS idx_skill_defs_scope ON skill_definitions(scope);
