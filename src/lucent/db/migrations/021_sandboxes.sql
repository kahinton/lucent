-- Migration 021: Sandbox tracking and task sandbox support
-- Persistent sandbox records + optional sandbox_config on tasks and schedules

-- Sandboxes table — tracks all sandbox lifecycle
CREATE TABLE IF NOT EXISTS sandboxes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'creating',
        -- creating, ready, running, stopped, failed, destroyed
    image VARCHAR(256) NOT NULL DEFAULT 'python:3.12-slim',
    repo_url TEXT,
    branch VARCHAR(128),
    config JSONB NOT NULL DEFAULT '{}',
        -- Full SandboxConfig as JSON (setup_commands, env_vars, resources, etc.)
    container_id VARCHAR(128),
    error TEXT,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    request_id UUID REFERENCES requests(id) ON DELETE SET NULL,
    organization_id UUID REFERENCES organizations(id),
    created_by UUID REFERENCES users(id),
    ready_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ,
    destroyed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sandboxes_status ON sandboxes(status);
CREATE INDEX IF NOT EXISTS idx_sandboxes_org ON sandboxes(organization_id);
CREATE INDEX IF NOT EXISTS idx_sandboxes_org_status ON sandboxes(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_sandboxes_task ON sandboxes(task_id);

-- Add sandbox_config JSONB to tasks (optional — daemon reads it before dispatch)
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sandbox_config JSONB;

-- Add sandbox_config JSONB to schedules (optional — copied to task on schedule trigger)
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sandbox_config JSONB;
