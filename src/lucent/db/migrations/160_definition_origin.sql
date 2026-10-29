-- Migration 160: Distinguish operator-provisioned definitions from disk-shipped ones.
--
-- ## The problem
--
-- Definitions already have `scope = 'built-in'`, which carries exactly the
-- semantics a hyperadmin-provisioned tool needs: consent-exempt, readable
-- org-wide, protected from daemon mutation, no per-user owner. So reusing it is
-- right — "anything added by the hyperadmin acts as a built-in" is precisely the
-- model.
--
-- But `scope` alone cannot express *where the row came from*, and that matters
-- because the built-in syncs reconcile against the on-disk catalog on **every
-- server start** with:
--
--     UPDATE <table> SET ... WHERE (name, organization_id) = ($1, $2)
--                            AND scope = 'built-in'
--
-- An operator row with `scope='built-in'` therefore gets **silently overwritten**
-- by a disk definition of the same name on the next restart — and conversely, if
-- an operator provisions a name the disk catalog does not carry, the managed-tool
-- prune (`_prune_retired_builtin_tools`) would **archive it** as "retired".
-- Both are wrong, and both fail quietly.
--
-- ## The fix
--
-- `origin` splits the one flag into the two facts it was conflating:
--
--   origin = 'disk'      -> shipped in .github/, owned by the sync. Reconciled,
--                           updatable, prunable.
--   origin = 'operator'  -> provisioned by an instance operator at runtime. Never
--                           touched by the sync; never pruned; updated only
--                           through the instance catalog API.
--
-- Both remain `scope = 'built-in'`, so every existing authorization, clearance,
-- consent, and UI rule applies unchanged. This is deliberately *not* a new scope
-- value: a new scope would require touching every ACL predicate in the codebase
-- and would give operators a different security profile from shipped built-ins,
-- which is the opposite of what we want.
--
-- Every existing built-in row is disk-shipped by definition (operators had no way
-- to create one before this), so they backfill to 'disk'.
--
-- Applied to the four definition families that can be granted to an agent. Hooks
-- are excluded: they are always disk-shipped today and have no operator story.

ALTER TABLE mcp_server_configs
    ADD COLUMN IF NOT EXISTS origin VARCHAR(16) NOT NULL DEFAULT 'disk';
ALTER TABLE managed_tool_definitions
    ADD COLUMN IF NOT EXISTS origin VARCHAR(16) NOT NULL DEFAULT 'disk';
ALTER TABLE skill_definitions
    ADD COLUMN IF NOT EXISTS origin VARCHAR(16) NOT NULL DEFAULT 'disk';
ALTER TABLE agent_definitions
    ADD COLUMN IF NOT EXISTS origin VARCHAR(16) NOT NULL DEFAULT 'disk';

-- A CHECK keeps the vocabulary closed, so a typo cannot create a third category
-- that silently matches neither branch of the sync predicate.
ALTER TABLE mcp_server_configs
    DROP CONSTRAINT IF EXISTS ck_mcp_cfg_origin;
ALTER TABLE mcp_server_configs
    ADD CONSTRAINT ck_mcp_cfg_origin CHECK (origin IN ('disk', 'operator'));

ALTER TABLE managed_tool_definitions
    DROP CONSTRAINT IF EXISTS ck_managed_tool_origin;
ALTER TABLE managed_tool_definitions
    ADD CONSTRAINT ck_managed_tool_origin CHECK (origin IN ('disk', 'operator'));

ALTER TABLE skill_definitions
    DROP CONSTRAINT IF EXISTS ck_skill_def_origin;
ALTER TABLE skill_definitions
    ADD CONSTRAINT ck_skill_def_origin CHECK (origin IN ('disk', 'operator'));

ALTER TABLE agent_definitions
    DROP CONSTRAINT IF EXISTS ck_agent_def_origin;
ALTER TABLE agent_definitions
    ADD CONSTRAINT ck_agent_def_origin CHECK (origin IN ('disk', 'operator'));

COMMENT ON COLUMN mcp_server_configs.origin IS
    'disk = shipped in .github/ and reconciled by the built-in sync; '
    'operator = provisioned at runtime by an instance operator. Both are '
    'scope=built-in; only disk rows are synced or pruned.';

-- Instance catalog listings are per-organization rows (one copy per tenant), so
-- the operator needs to find them across tenants efficiently.
CREATE INDEX IF NOT EXISTS idx_mcp_cfg_origin
    ON mcp_server_configs(origin) WHERE origin = 'operator';
CREATE INDEX IF NOT EXISTS idx_managed_tool_defs_origin
    ON managed_tool_definitions(origin) WHERE origin = 'operator';
CREATE INDEX IF NOT EXISTS idx_skill_defs_origin
    ON skill_definitions(origin) WHERE origin = 'operator';
CREATE INDEX IF NOT EXISTS idx_agent_defs_origin
    ON agent_definitions(origin) WHERE origin = 'operator';
