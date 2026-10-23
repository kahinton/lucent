-- Migration 147: Per-user consent for capabilities attached to an agent.
--
-- The agent is the structural hub for capability access. A skill, managed tool,
-- hook, or MCP server reaches a running agent in two steps:
--
--   1. it is *attached* to the agent (the agent_* junction tables), and
--   2. the *requesting user* is allowed to use it.
--
-- Step 2 is an intersection, which is what makes the model multi-tenant safe:
-- a capability attached to a shared agent is only visible to a user who can
-- already read that capability. Alice's private "Pep Talk" skill attached to a
-- shared agent never appears for Bob.
--
-- The intersection alone is not enough. A user who can read a capability can
-- still be handed it, by someone else, inside an agent they trust — that is how
-- a malicious hook or managed tool gets injected into someone else's agent.
-- This table records that consent per (user, agent, capability). Rows are
-- written when a user approves or declines; absence means "not yet asked".
--
-- Consent is NOT required for:
--   * built-in capabilities (they are reviewed, and every org member already
--     holds an automatic org-wide read clearance for them),
--   * the user who attached the capability,
--   * the capability's owner (or a member of the owning group).
-- Those are resolved in code, not stored here, so a capability transferred to a
-- new owner needs no consent backfill.
--
-- 'declined' is sticky: a declined capability never re-prompts on its own. The
-- user can re-approve explicitly from their approvals panel.
--
-- No data backfill ships with this migration. Pre-existing attachments on a
-- live instance are grandfathered with a one-off operator statement; a fresh
-- install has no legacy attachments to grandfather.

CREATE TABLE IF NOT EXISTS agent_capability_consents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    agent_id UUID NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    capability_type VARCHAR(16) NOT NULL,
    skill_id UUID REFERENCES skill_definitions(id) ON DELETE CASCADE,
    managed_tool_id UUID REFERENCES managed_tool_definitions(id) ON DELETE CASCADE,
    hook_id UUID REFERENCES hook_definitions(id) ON DELETE CASCADE,
    mcp_server_id UUID REFERENCES mcp_server_configs(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state VARCHAR(16) NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE agent_capability_consents
    DROP CONSTRAINT IF EXISTS agent_capability_consents_capability_type_check;
ALTER TABLE agent_capability_consents
    ADD CONSTRAINT agent_capability_consents_capability_type_check
    CHECK (capability_type IN ('skill', 'managed_tool', 'hook', 'mcp_server'));

ALTER TABLE agent_capability_consents
    DROP CONSTRAINT IF EXISTS agent_capability_consents_state_check;
ALTER TABLE agent_capability_consents
    ADD CONSTRAINT agent_capability_consents_state_check
    CHECK (state IN ('approved', 'declined'));

-- Exactly one target column may be set, and it must match capability_type.
ALTER TABLE agent_capability_consents
    DROP CONSTRAINT IF EXISTS ck_agent_capability_consent_target;
ALTER TABLE agent_capability_consents
    ADD CONSTRAINT ck_agent_capability_consent_target
    CHECK (
        (capability_type = 'skill'
            AND skill_id IS NOT NULL
            AND managed_tool_id IS NULL AND hook_id IS NULL AND mcp_server_id IS NULL)
        OR (capability_type = 'managed_tool'
            AND managed_tool_id IS NOT NULL
            AND skill_id IS NULL AND hook_id IS NULL AND mcp_server_id IS NULL)
        OR (capability_type = 'hook'
            AND hook_id IS NOT NULL
            AND skill_id IS NULL AND managed_tool_id IS NULL AND mcp_server_id IS NULL)
        OR (capability_type = 'mcp_server'
            AND mcp_server_id IS NOT NULL
            AND skill_id IS NULL AND managed_tool_id IS NULL AND hook_id IS NULL)
    );

-- One decision per (user, agent, capability). Partial indexes because the four
-- target columns are mutually exclusive and nullable.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_capability_consent_skill
    ON agent_capability_consents(agent_id, user_id, skill_id)
    WHERE capability_type = 'skill';

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_capability_consent_managed_tool
    ON agent_capability_consents(agent_id, user_id, managed_tool_id)
    WHERE capability_type = 'managed_tool';

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_capability_consent_hook
    ON agent_capability_consents(agent_id, user_id, hook_id)
    WHERE capability_type = 'hook';

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_capability_consent_mcp_server
    ON agent_capability_consents(agent_id, user_id, mcp_server_id)
    WHERE capability_type = 'mcp_server';

-- Pending/declined listing and the navigation badge count both scan by
-- (organization, user, state).
CREATE INDEX IF NOT EXISTS idx_agent_capability_consents_user_state
    ON agent_capability_consents(organization_id, user_id, state);

CREATE INDEX IF NOT EXISTS idx_agent_capability_consents_agent_user
    ON agent_capability_consents(agent_id, user_id);

-- Attach provenance. This exists for exactly one purpose: the user who attached
-- a capability does not have to approve their own attachment. It is NOT
-- revocation authority (that is the agent owner, the capability owner, or an org
-- admin/owner) and NOT an audit trail (admin_audit_log covers that).
--
-- NULL means system-originated: built-in sync and default-hook seeding. Those
-- capabilities are built-in scope, which is already exempt from consent.
ALTER TABLE agent_skills
    ADD COLUMN IF NOT EXISTS granted_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE agent_managed_tools
    ADD COLUMN IF NOT EXISTS granted_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE agent_hooks
    ADD COLUMN IF NOT EXISTS granted_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE agent_mcp_servers
    ADD COLUMN IF NOT EXISTS granted_by UUID REFERENCES users(id) ON DELETE SET NULL;

COMMENT ON TABLE agent_capability_consents IS
  'Per-user approval for a capability attached to an agent. Missing row = not yet asked; consent exemptions (built-in, grantor, owner) are resolved in code.';
COMMENT ON COLUMN agent_capability_consents.state IS
  'approved or declined. Declined is sticky and never re-prompts automatically.';
COMMENT ON COLUMN agent_skills.granted_by IS
  'User who attached the skill. Consent exemption only, not revocation authority.';
COMMENT ON COLUMN agent_managed_tools.granted_by IS
  'User who attached the managed tool. Consent exemption only, not revocation authority.';
COMMENT ON COLUMN agent_hooks.granted_by IS
  'User who attached the hook. Consent exemption only, not revocation authority.';
COMMENT ON COLUMN agent_mcp_servers.granted_by IS
  'User who attached the MCP server. Consent exemption only, not revocation authority.';
