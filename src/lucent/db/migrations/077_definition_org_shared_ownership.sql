-- Migration 077: Organization-shared definition ownership
--
-- Definitions can now be owned by exactly one of:
--   1. a user (owner_user_id set)
--   2. a group (owner_group_id set)
--   3. the organization (both owner columns NULL on an instance-scoped row)
-- Built-ins remain scope='built-in' and are globally visible inside the org.

-- Relax the earlier owner-required checks. NULL/NULL on an instance row now
-- means "shared with the entire organization", not an orphaned definition.
ALTER TABLE agent_definitions DROP CONSTRAINT IF EXISTS ck_agent_def_owner_or_builtin;
ALTER TABLE skill_definitions DROP CONSTRAINT IF EXISTS ck_skill_def_owner_or_builtin;
ALTER TABLE mcp_server_configs DROP CONSTRAINT IF EXISTS ck_mcp_cfg_owner_or_builtin;
ALTER TABLE sandbox_templates DROP CONSTRAINT IF EXISTS ck_sandbox_tpl_owner_or_builtin;

-- Make daemon-owned instance definitions org-shared. The daemon is an actor,
-- not a sensible end-user owner for capabilities humans should browse/use.
UPDATE agent_definitions d
SET owner_user_id = NULL,
    owner_group_id = NULL,
    updated_at = NOW()
FROM users u
WHERE d.owner_user_id = u.id
  AND u.role = 'daemon'
  AND d.scope = 'instance';

UPDATE skill_definitions d
SET owner_user_id = NULL,
    owner_group_id = NULL,
    updated_at = NOW()
FROM users u
WHERE d.owner_user_id = u.id
  AND u.role = 'daemon'
  AND d.scope = 'instance';

UPDATE mcp_server_configs d
SET owner_user_id = NULL,
    owner_group_id = NULL,
    updated_at = NOW()
FROM users u
WHERE d.owner_user_id = u.id
  AND u.role = 'daemon'
  AND d.scope = 'instance';

UPDATE hook_definitions d
SET owner_user_id = NULL,
    owner_group_id = NULL,
    updated_at = NOW()
FROM users u
WHERE d.owner_user_id = u.id
  AND u.role = 'daemon'
  AND d.scope = 'instance';

UPDATE sandbox_templates d
SET owner_user_id = NULL,
    owner_group_id = NULL,
    updated_at = NOW()
FROM users u
WHERE d.owner_user_id = u.id
  AND u.role = 'daemon'
  AND d.scope = 'instance';

-- Enforce single-owner semantics for user/group ownership. Org-shared rows are
-- represented by NULL/NULL and are valid for instance-scoped definitions.
ALTER TABLE agent_definitions
    ADD CONSTRAINT ck_agent_def_single_owner
    CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL))
    NOT VALID;

ALTER TABLE skill_definitions
    ADD CONSTRAINT ck_skill_def_single_owner
    CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL))
    NOT VALID;

ALTER TABLE mcp_server_configs
    ADD CONSTRAINT ck_mcp_cfg_single_owner
    CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL))
    NOT VALID;

ALTER TABLE hook_definitions
    ADD CONSTRAINT ck_hook_def_single_owner
    CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL))
    NOT VALID;

ALTER TABLE sandbox_templates
    ADD CONSTRAINT ck_sandbox_tpl_single_owner
    CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL))
    NOT VALID;

CREATE INDEX IF NOT EXISTS idx_agent_def_org_shared
    ON agent_definitions(organization_id, status, name)
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_skill_def_org_shared
    ON skill_definitions(organization_id, status, name)
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_cfg_org_shared
    ON mcp_server_configs(organization_id, status, name)
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_hook_def_org_shared
    ON hook_definitions(organization_id, status, name)
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL;

COMMENT ON COLUMN agent_definitions.owner_user_id IS 'User owner. NULL for built-in, group-owned, or organization-shared definitions.';
COMMENT ON COLUMN agent_definitions.owner_group_id IS 'Group owner. NULL for built-in, user-owned, or organization-shared definitions.';
COMMENT ON COLUMN skill_definitions.owner_user_id IS 'User owner. NULL for built-in, group-owned, or organization-shared definitions.';
COMMENT ON COLUMN skill_definitions.owner_group_id IS 'Group owner. NULL for built-in, user-owned, or organization-shared definitions.';
COMMENT ON COLUMN mcp_server_configs.owner_user_id IS 'User owner. NULL for built-in, group-owned, or organization-shared definitions.';
COMMENT ON COLUMN mcp_server_configs.owner_group_id IS 'Group owner. NULL for built-in, user-owned, or organization-shared definitions.';
COMMENT ON COLUMN hook_definitions.owner_user_id IS 'User owner. NULL for built-in, group-owned, or organization-shared definitions.';
COMMENT ON COLUMN hook_definitions.owner_group_id IS 'Group owner. NULL for built-in, user-owned, or organization-shared definitions.';
COMMENT ON COLUMN sandbox_templates.owner_user_id IS 'User owner. NULL for built-in, group-owned, or organization-shared templates.';
COMMENT ON COLUMN sandbox_templates.owner_group_id IS 'Group owner. NULL for built-in, user-owned, or organization-shared templates.';
