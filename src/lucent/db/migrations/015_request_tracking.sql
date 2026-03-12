-- Migration 015: Request tracking and task queue system
-- Provides full lineage: request → tasks → events → memory links

-- ── Requests: top-level work items ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(256) NOT NULL,
    description TEXT,
    source VARCHAR(32) NOT NULL DEFAULT 'user',  -- 'user', 'cognitive', 'api', 'daemon'
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
        -- 'pending', 'planning', 'in_progress', 'completed', 'failed', 'cancelled'
    priority VARCHAR(8) NOT NULL DEFAULT 'medium',  -- 'low', 'medium', 'high', 'urgent'
    created_by UUID REFERENCES users(id),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Tasks: individual units of work within a request ────────────────────

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    parent_task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,  -- sub-tasks
    title VARCHAR(256) NOT NULL,
    description TEXT,
    agent_type VARCHAR(32),                           -- e.g. 'code', 'research', 'security'
    agent_definition_id UUID REFERENCES agent_definitions(id),  -- optional instance agent
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
        -- 'pending', 'planned', 'claimed', 'running', 'completed', 'failed', 'cancelled', 'needs_review'
    priority VARCHAR(8) NOT NULL DEFAULT 'medium',
    sequence_order INT DEFAULT 0,                     -- execution order within request
    result TEXT,                                      -- agent output
    error TEXT,                                       -- failure reason if failed
    claimed_by VARCHAR(64),                           -- daemon instance ID
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    organization_id UUID NOT NULL REFERENCES organizations(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Task Events: real-time progress log ─────────────────────────────────

CREATE TABLE IF NOT EXISTS task_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type VARCHAR(32) NOT NULL,
        -- 'created', 'planned', 'claimed', 'progress', 'running',
        -- 'completed', 'failed', 'cancelled', 'needs_review',
        -- 'review_approved', 'review_rejected',
        -- 'memory_created', 'memory_read', 'memory_updated',
        -- 'sub_task_created', 'agent_dispatched', 'agent_completed'
    detail TEXT,                                      -- human-readable description
    metadata JSONB DEFAULT '{}',                      -- extra data (memory_id, model, etc.)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Task ↔ Memory links: which memories were touched ────────────────────

CREATE TABLE IF NOT EXISTS task_memories (
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation VARCHAR(16) NOT NULL DEFAULT 'created',  -- 'created', 'read', 'updated'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, memory_id, relation)
);

-- ── Indexes ─────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_requests_org_status ON requests(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_request ON tasks(request_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_org_status ON tasks(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_claimed ON tasks(claimed_by) WHERE claimed_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_events_type ON task_events(event_type);
CREATE INDEX IF NOT EXISTS idx_task_memories_task ON task_memories(task_id);
CREATE INDEX IF NOT EXISTS idx_task_memories_memory ON task_memories(memory_id);
