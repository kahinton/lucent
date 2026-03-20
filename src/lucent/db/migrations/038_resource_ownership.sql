-- Migration 038: Resource ownership columns
-- Phase 2 of the security upgrade: adds owner_user_id and owner_group_id
-- to all definition tables and sandbox_templates for group-based access control.
--
-- References consensus design (memory 965e62d4) and GPT cross-model review.
-- Both columns are nullable — built-in resources have NULL owners intentionally.
-- CHECK constraints use NOT VALID for safe two-phase rollout (GPT review rec #7).

-- ── Agent definitions ────────────────────────────────────────────────────

ALTER TABLE agent_definitions
    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_group_id UUID REFERENCES groups(id) ON DELETE SET NULL;

-- ── Skill definitions ────────────────────────────────────────────────────

ALTER TABLE skill_definitions
    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_group_id UUID REFERENCES groups(id) ON DELETE SET NULL;

-- ── MCP server configs ───────────────────────────────────────────────────

ALTER TABLE mcp_server_configs
    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_group_id UUID REFERENCES groups(id) ON DELETE SET NULL;

-- ── Sandbox templates ────────────────────────────────────────────────────

-- Add scope column for consistency with other definition tables
ALTER TABLE sandbox_templates
    ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'instance';

ALTER TABLE sandbox_templates
    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_group_id UUID REFERENCES groups(id) ON DELETE SET NULL;

-- ── Backfill: set owner_user_id = created_by for existing instance-scoped rows ──

UPDATE agent_definitions
    SET owner_user_id = created_by
    WHERE (scope = 'instance' OR scope IS NULL)
    AND created_by IS NOT NULL
    AND owner_user_id IS NULL;

UPDATE skill_definitions
    SET owner_user_id = created_by
    WHERE (scope = 'instance' OR scope IS NULL)
    AND created_by IS NOT NULL
    AND owner_user_id IS NULL;

UPDATE mcp_server_configs
    SET owner_user_id = created_by
    WHERE (scope = 'instance' OR scope IS NULL)
    AND created_by IS NOT NULL
    AND owner_user_id IS NULL;

UPDATE sandbox_templates
    SET owner_user_id = created_by
    WHERE (scope = 'instance' OR scope IS NULL)
    AND created_by IS NOT NULL
    AND owner_user_id IS NULL;

-- ── CHECK constraints (NOT VALID — enforced on new rows, existing validated separately) ──
-- Per GPT review: scope='built-in' exempted from ownership requirement.

ALTER TABLE agent_definitions
    ADD CONSTRAINT ck_agent_def_owner_or_builtin
    CHECK (scope = 'built-in' OR owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL)
    NOT VALID;

ALTER TABLE skill_definitions
    ADD CONSTRAINT ck_skill_def_owner_or_builtin
    CHECK (scope = 'built-in' OR owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL)
    NOT VALID;

ALTER TABLE mcp_server_configs
    ADD CONSTRAINT ck_mcp_cfg_owner_or_builtin
    CHECK (scope = 'built-in' OR owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL)
    NOT VALID;

ALTER TABLE sandbox_templates
    ADD CONSTRAINT ck_sandbox_tpl_owner_or_builtin
    CHECK (scope = 'built-in' OR owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL)
    NOT VALID;

-- ── Indexes for ACL hot path (per GPT review recommendations) ────────────

CREATE INDEX IF NOT EXISTS idx_agent_def_owner_user
    ON agent_definitions(organization_id, owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_def_owner_group
    ON agent_definitions(organization_id, owner_group_id) WHERE owner_group_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_skill_def_owner_user
    ON skill_definitions(organization_id, owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_skill_def_owner_group
    ON skill_definitions(organization_id, owner_group_id) WHERE owner_group_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mcp_cfg_owner_user
    ON mcp_server_configs(organization_id, owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_cfg_owner_group
    ON mcp_server_configs(organization_id, owner_group_id) WHERE owner_group_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sandbox_tpl_owner_user
    ON sandbox_templates(organization_id, owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sandbox_tpl_owner_group
    ON sandbox_templates(organization_id, owner_group_id) WHERE owner_group_id IS NOT NULL;

-- ── Column comments ──────────────────────────────────────────────────────

COMMENT ON COLUMN agent_definitions.owner_user_id IS 'User who owns this resource. NULL for built-in resources.';
COMMENT ON COLUMN agent_definitions.owner_group_id IS 'Group that owns this resource. NULL for user-owned or built-in resources.';
COMMENT ON COLUMN skill_definitions.owner_user_id IS 'User who owns this resource. NULL for built-in resources.';
COMMENT ON COLUMN skill_definitions.owner_group_id IS 'Group that owns this resource. NULL for user-owned or built-in resources.';
COMMENT ON COLUMN mcp_server_configs.owner_user_id IS 'User who owns this resource. NULL for built-in resources.';
COMMENT ON COLUMN mcp_server_configs.owner_group_id IS 'Group that owns this resource. NULL for user-owned or built-in resources.';
COMMENT ON COLUMN sandbox_templates.owner_user_id IS 'User who owns this resource. NULL for built-in resources.';
COMMENT ON COLUMN sandbox_templates.owner_group_id IS 'Group that owns this resource. NULL for user-owned or built-in resources.';
COMMENT ON COLUMN sandbox_templates.scope IS 'built-in = shipped with platform, instance = created by users';
