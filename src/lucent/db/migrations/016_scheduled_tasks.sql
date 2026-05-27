-- Migration 016: Scheduled Tasks
-- Supports one-time and repeating schedules with full tracking

CREATE TABLE IF NOT EXISTS schedules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    created_by UUID REFERENCES users(id),

    -- What to do
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    agent_type TEXT NOT NULL DEFAULT 'code',
    task_template JSONB DEFAULT '{}',  -- template fields passed when creating the request/task

    -- When to do it
    schedule_type TEXT NOT NULL DEFAULT 'once',  -- 'once', 'interval', 'cron'
    cron_expression TEXT,          -- e.g. '0 9 * * 1' (9am every Monday)
    interval_seconds INTEGER,      -- e.g. 3600 for hourly
    next_run_at TIMESTAMPTZ,       -- when the next execution should happen
    last_run_at TIMESTAMPTZ,       -- when it last executed
    run_count INTEGER DEFAULT 0,   -- how many times it has run
    max_runs INTEGER,              -- optional cap (NULL = unlimited)

    -- Behavior
    priority TEXT DEFAULT 'medium',
    enabled BOOLEAN DEFAULT true,
    timezone TEXT DEFAULT 'UTC',
    
    -- Lifecycle
    status TEXT DEFAULT 'active',  -- 'active', 'paused', 'completed', 'expired'
    expires_at TIMESTAMPTZ,        -- optional expiration
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    CONSTRAINT valid_schedule_type CHECK (schedule_type IN ('once', 'interval', 'cron')),
    CONSTRAINT valid_status CHECK (status IN ('active', 'paused', 'completed', 'expired')),
    CONSTRAINT valid_priority CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    CONSTRAINT cron_requires_expression CHECK (
        schedule_type != 'cron' OR cron_expression IS NOT NULL
    ),
    CONSTRAINT interval_requires_seconds CHECK (
        schedule_type != 'interval' OR interval_seconds IS NOT NULL
    )
);

-- Track each run of a schedule (links to the request tracking system)
CREATE TABLE IF NOT EXISTS schedule_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    schedule_id UUID NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    request_id UUID REFERENCES requests(id),  -- the request created for this run
    
    status TEXT DEFAULT 'pending',  -- 'pending', 'running', 'completed', 'failed', 'skipped'
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result TEXT,
    error TEXT,
    
    created_at TIMESTAMPTZ DEFAULT now(),
    
    CONSTRAINT valid_run_status CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_schedules_org ON schedules(organization_id);
CREATE INDEX IF NOT EXISTS idx_schedules_next_run ON schedules(next_run_at) WHERE enabled = true AND status = 'active';
CREATE INDEX IF NOT EXISTS idx_schedules_status ON schedules(status, enabled);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule ON schedule_runs(schedule_id);
CREATE INDEX IF NOT EXISTS idx_schedule_runs_request ON schedule_runs(request_id);

-- Migration applied
