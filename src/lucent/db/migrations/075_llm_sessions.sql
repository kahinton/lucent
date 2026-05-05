-- Migration 075: Persist LLM/chat sessions and request lineage
-- Adds Lucent-owned session records that can mirror provider session IDs
-- (Copilot SDK sessions) while keeping provider-independent transcripts for
-- LangChain and other engines.

CREATE TABLE IF NOT EXISTS llm_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    kind VARCHAR(32) NOT NULL DEFAULT 'chat',
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    title VARCHAR(256),
    summary TEXT,
    engine VARCHAR(32),
    model VARCHAR(128),
    reasoning_effort VARCHAR(64),
    agent_definition_id UUID REFERENCES agent_definitions(id) ON DELETE SET NULL,
    provider_session_id TEXT,
    provider_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_id UUID REFERENCES requests(id) ON DELETE SET NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    schedule_run_id UUID REFERENCES schedule_runs(id) ON DELETE SET NULL,
    parent_session_id UUID REFERENCES llm_sessions(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_message_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_llm_sessions_kind CHECK (
        kind IN (
            'chat', 'embedded_chat', 'task', 'request', 'daemon',
            'schedule', 'integration'
        )
    ),
    CONSTRAINT chk_llm_sessions_status CHECK (
        status IN ('active', 'idle', 'archived', 'deleted', 'error')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_llm_sessions_org_provider
    ON llm_sessions (organization_id, provider_session_id)
    WHERE provider_session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_llm_sessions_org_user_recent
    ON llm_sessions (organization_id, user_id, last_message_at DESC NULLS LAST, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_sessions_org_kind_recent
    ON llm_sessions (organization_id, kind, last_message_at DESC NULLS LAST, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_sessions_request
    ON llm_sessions (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_sessions_task
    ON llm_sessions (task_id) WHERE task_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS llm_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES llm_sessions(id) ON DELETE CASCADE,
    turn_id UUID,
    sequence INTEGER NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_llm_messages_role CHECK (
        role IN ('system', 'user', 'assistant', 'tool')
    ),
    CONSTRAINT uniq_llm_messages_session_sequence UNIQUE (session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_llm_messages_session_sequence
    ON llm_messages (session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_llm_messages_turn
    ON llm_messages (turn_id) WHERE turn_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS llm_session_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES llm_sessions(id) ON DELETE CASCADE,
    message_id UUID REFERENCES llm_messages(id) ON DELETE SET NULL,
    turn_id UUID,
    sequence INTEGER NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    tool_name TEXT,
    tool_input JSONB,
    tool_output JSONB,
    detail TEXT,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    visible BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uniq_llm_session_events_session_sequence UNIQUE (session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_llm_session_events_session_sequence
    ON llm_session_events (session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_llm_session_events_turn
    ON llm_session_events (turn_id) WHERE turn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_session_events_type
    ON llm_session_events (event_type);
CREATE INDEX IF NOT EXISTS idx_llm_session_events_tool
    ON llm_session_events (tool_name) WHERE tool_name IS NOT NULL;

CREATE TABLE IF NOT EXISTS llm_session_requests (
    session_id UUID NOT NULL REFERENCES llm_sessions(id) ON DELETE CASCADE,
    request_id UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    message_id UUID REFERENCES llm_messages(id) ON DELETE SET NULL,
    event_id UUID REFERENCES llm_session_events(id) ON DELETE SET NULL,
    relation VARCHAR(16) NOT NULL DEFAULT 'created',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, request_id, relation),
    CONSTRAINT chk_llm_session_requests_relation CHECK (
        relation IN ('created', 'discussed', 'reviewed', 'handoff')
    )
);

CREATE INDEX IF NOT EXISTS idx_llm_session_requests_request
    ON llm_session_requests (request_id);

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS origin_session_id UUID REFERENCES llm_sessions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS origin_message_id UUID REFERENCES llm_messages(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS origin_event_id UUID REFERENCES llm_session_events(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_requests_origin_session
    ON requests (origin_session_id) WHERE origin_session_id IS NOT NULL;
