-- Migration 088: Grant metadata (grantor provenance) on agent capability junctions
-- Records WHO granted a capability to an agent, WHEN, WHY, and whether the grant
-- overrode a scope-compatibility warning. This makes grants auditable and lets the
-- access-control plane detect and rectify ownership/scope mismatches (see
-- docs/access-control.md).

ALTER TABLE agent_skills
    ADD COLUMN IF NOT EXISTS granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS grant_reason TEXT,
    ADD COLUMN IF NOT EXISTS grant_override BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE agent_mcp_servers
    ADD COLUMN IF NOT EXISTS granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS grant_reason TEXT,
    ADD COLUMN IF NOT EXISTS grant_override BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE agent_hooks
    ADD COLUMN IF NOT EXISTS granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS grant_reason TEXT,
    ADD COLUMN IF NOT EXISTS grant_override BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE agent_managed_tools
    ADD COLUMN IF NOT EXISTS granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS grant_reason TEXT,
    ADD COLUMN IF NOT EXISTS grant_override BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN agent_skills.granted_by IS 'User who created this grant (grantor). NULL if user deleted or system-seeded.';
COMMENT ON COLUMN agent_skills.grant_reason IS 'Optional justification supplied at grant time; required when grant_override is true.';
COMMENT ON COLUMN agent_skills.grant_override IS 'True when the grant was made despite a scope-compatibility warning.';
COMMENT ON COLUMN agent_mcp_servers.granted_by IS 'User who created this grant (grantor). NULL if user deleted or system-seeded.';
COMMENT ON COLUMN agent_mcp_servers.grant_reason IS 'Optional justification supplied at grant time; required when grant_override is true.';
COMMENT ON COLUMN agent_mcp_servers.grant_override IS 'True when the grant was made despite a scope-compatibility warning.';
COMMENT ON COLUMN agent_hooks.granted_by IS 'User who created this grant (grantor). NULL if user deleted or system-seeded.';
COMMENT ON COLUMN agent_hooks.grant_reason IS 'Optional justification supplied at grant time; required when grant_override is true.';
COMMENT ON COLUMN agent_hooks.grant_override IS 'True when the grant was made despite a scope-compatibility warning.';
COMMENT ON COLUMN agent_managed_tools.granted_by IS 'User who created this grant (grantor). NULL if user deleted or system-seeded.';
COMMENT ON COLUMN agent_managed_tools.grant_reason IS 'Optional justification supplied at grant time; required when grant_override is true.';
COMMENT ON COLUMN agent_managed_tools.grant_override IS 'True when the grant was made despite a scope-compatibility warning.';
