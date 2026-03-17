-- Migration 022: Sandbox templates (reusable environment definitions)

CREATE TABLE IF NOT EXISTS sandbox_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(128) NOT NULL,
    description TEXT DEFAULT '',

    -- Environment definition
    image VARCHAR(256) NOT NULL DEFAULT 'python:3.12-slim',
    repo_url TEXT,
    branch VARCHAR(128) DEFAULT 'main',
    setup_commands JSONB DEFAULT '[]',     -- ["pip install ...", ...]
    env_vars JSONB DEFAULT '{}',           -- {"KEY": "VALUE", ...}
    working_dir VARCHAR(256) DEFAULT '/workspace',

    -- Resources
    memory_limit VARCHAR(16) DEFAULT '2g',
    cpu_limit NUMERIC(4,1) DEFAULT 2.0,
    disk_limit VARCHAR(16) DEFAULT '10g',

    -- Network
    network_mode VARCHAR(16) DEFAULT 'none',    -- none, bridge, allowlist
    allowed_hosts JSONB DEFAULT '[]',

    -- Lifecycle
    timeout_seconds INTEGER DEFAULT 1800,

    -- Ownership
    organization_id UUID NOT NULL REFERENCES organizations(id),
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(name, organization_id)
);

CREATE INDEX IF NOT EXISTS idx_sandbox_templates_org ON sandbox_templates(organization_id);

-- Add template reference to tasks and schedules (preferred over inline sandbox_config)
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sandbox_template_id UUID REFERENCES sandbox_templates(id);
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sandbox_template_id UUID REFERENCES sandbox_templates(id);

-- Add template reference to sandbox instances (what template was it created from)
ALTER TABLE sandboxes ADD COLUMN IF NOT EXISTS template_id UUID REFERENCES sandbox_templates(id);
