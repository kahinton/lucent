-- Migration 076: Agent hooks
-- Adds hook definitions and agent↔hook grants. Hooks are approved definition
-- objects, like skills and MCP servers, that execute as runtime middleware
-- around model/tool events. Command hooks run out-of-process with runtime
-- limits after approval.

CREATE TABLE IF NOT EXISTS hook_definitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(64) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    trigger_event VARCHAR(64) NOT NULL DEFAULT 'tool_call',
    action_type VARCHAR(64) NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(16) NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'active', 'rejected')),
    scope VARCHAR(16) NOT NULL DEFAULT 'instance'
        CHECK (scope IN ('instance', 'built-in')),
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    owner_group_id UUID REFERENCES groups(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, organization_id)
);

CREATE INDEX IF NOT EXISTS idx_hook_definitions_org_status
    ON hook_definitions(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_hook_definitions_owner_user
    ON hook_definitions(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_hook_definitions_owner_group
    ON hook_definitions(owner_group_id);

CREATE TABLE IF NOT EXISTS agent_hooks (
    agent_id UUID NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    hook_id UUID NOT NULL REFERENCES hook_definitions(id) ON DELETE CASCADE,
    config_override JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, hook_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_hooks_hook ON agent_hooks(hook_id);

COMMENT ON TABLE hook_definitions IS 'Approved runtime hooks/middleware that can observe agent events, inject context, or run bounded command actions.';
COMMENT ON COLUMN hook_definitions.trigger_event IS 'Lifecycle event that triggers the hook: before_model_call, after_model_call, before_tool_call, after_tool_call, or legacy tool_call.';
COMMENT ON COLUMN hook_definitions.action_type IS 'Action implementation, e.g. memory_lookup, static_context, or command.';
COMMENT ON COLUMN hook_definitions.config IS 'JSON configuration for matching and action behavior. Command hooks may specify command, timeout_seconds, max_output_chars, env, cwd, pass_input, and can emit JSON decisions: allow, inject, block, replace_args, replace_result.';
COMMENT ON TABLE agent_hooks IS 'Junction table granting hook definitions to agent definitions.';
