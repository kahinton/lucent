-- Migration 079: Tool call audit log
--
-- Stores structured tool-use telemetry for learning and reliability analysis.
-- This is intentionally separate from memories: rows are operational audit data,
-- not user/agent knowledge. Keep payloads summarized/redacted so secrets and
-- large content bodies do not become analytics exhaust.

CREATE TABLE IF NOT EXISTS tool_call_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    api_key_id UUID REFERENCES api_keys(id) ON DELETE SET NULL,

    session_id UUID REFERENCES llm_sessions(id) ON DELETE SET NULL,
    turn_id UUID,
    message_id UUID REFERENCES llm_messages(id) ON DELETE SET NULL,
    llm_event_id UUID REFERENCES llm_session_events(id) ON DELETE SET NULL,

    request_id UUID REFERENCES requests(id) ON DELETE SET NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    schedule_run_id UUID REFERENCES schedule_runs(id) ON DELETE SET NULL,

    agent_definition_id UUID REFERENCES agent_definitions(id) ON DELETE SET NULL,
    agent_type TEXT,
    skill_names TEXT[],

    model TEXT,
    reasoning_effort TEXT,
    engine TEXT,
    provider TEXT,
    source TEXT NOT NULL DEFAULT 'unknown',

    tool_name TEXT NOT NULL,
    tool_namespace TEXT,
    tool_call_id TEXT,
    mcp_server TEXT,

    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'blocked')),
    failure_class TEXT,
    error_message TEXT,
    error_code TEXT,
    duration_ms INTEGER,

    input_preview JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_preview TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tool_call_audit_org_created
    ON tool_call_audit_log(organization_id, created_at DESC)
    WHERE organization_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_call_audit_status_tool
    ON tool_call_audit_log(status, tool_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_call_audit_session_created
    ON tool_call_audit_log(session_id, created_at DESC)
    WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_call_audit_request_created
    ON tool_call_audit_log(request_id, created_at DESC)
    WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_call_audit_task_created
    ON tool_call_audit_log(task_id, created_at DESC)
    WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_call_audit_agent_model
    ON tool_call_audit_log(organization_id, agent_type, model, status, created_at DESC)
    WHERE organization_id IS NOT NULL;

COMMENT ON TABLE tool_call_audit_log IS
  'Structured operational audit log for LLM/agent tool calls. Complements learning memories but is not stored as memory content.';
COMMENT ON COLUMN tool_call_audit_log.input_preview IS
  'Redacted/truncated JSON preview of tool arguments for analytics; never intended as full payload storage.';
COMMENT ON COLUMN tool_call_audit_log.output_preview IS
  'Redacted/truncated text preview of tool result or failure for diagnostics.';
