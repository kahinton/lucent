-- Migration 126: Clearances for the definition family (agents, skills,
-- MCP servers, hooks, managed tools).
--
-- Definition *use* moves to clearance-driven, default-deny reads (pickers,
-- task/schedule dropdowns, definition tools); management surfaces (definitions
-- UI, approvals, grant endpoints) stay on the org-scoped path with their
-- existing admin gates.
--
-- Built-ins: they exist specifically to be available to the whole org, so each
-- built-in row gets an organization-wide 'read' clearance at birth. Because
-- that grant is a normal auth_clearances row (not a code predicate), an admin
-- can later remove it to narrow availability — the startup sync only UPDATEs
-- existing built-ins (never re-INSERTs), so a removed grant stays removed.
--
-- Orphaned instances: instance rows with no owner were org-visible under the
-- legacy predicate. Per decision (2026-09-30), the current org owner becomes
-- their owner — the 123 transfer trigger files the owner clearance — and the
-- admin can re-share from there. Daemon-created definitions should carry an
-- owner from creation going forward.

-- ---------------------------------------------------------------------------
-- 1. Built-in = "everyone in this org can use this", granted automatically.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.grant_auth_org_read_for_builtin()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NEW.scope <> 'built-in'
       OR NEW.organization_id IS NULL
       OR NEW.auth_id IS NULL THEN
        RETURN NEW;
    END IF;

    INSERT INTO auth_clearances
        (auth_id, role, principal_type, principal_id, granted_by)
    VALUES
        (NEW.auth_id, 'read', 'org', NEW.organization_id, NULL)
    ON CONFLICT (auth_id, principal_type, principal_id, role)
    DO NOTHING;

    RETURN NEW;
END;
$$;

CREATE TRIGGER auth_builtin_org_read
AFTER INSERT ON public.agent_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_org_read_for_builtin();

CREATE TRIGGER auth_builtin_org_read
AFTER INSERT ON public.skill_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_org_read_for_builtin();

CREATE TRIGGER auth_builtin_org_read
AFTER INSERT ON public.mcp_server_configs
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_org_read_for_builtin();

CREATE TRIGGER auth_builtin_org_read
AFTER INSERT ON public.hook_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_org_read_for_builtin();

CREATE TRIGGER auth_builtin_org_read
AFTER INSERT ON public.managed_tool_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_org_read_for_builtin();

-- ---------------------------------------------------------------------------
-- 2. Managed tools were missed by migration 123's ownership triggers.
-- ---------------------------------------------------------------------------

CREATE TRIGGER auth_owner_clearance
AFTER INSERT ON public.managed_tool_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_owner_for_row('owner_user_id');

CREATE TRIGGER auth_owner_clearance_update
AFTER UPDATE ON public.managed_tool_definitions
FOR EACH ROW
WHEN (OLD.owner_user_id IS DISTINCT FROM NEW.owner_user_id)
EXECUTE FUNCTION public.transfer_auth_owner_for_row('owner_user_id');

-- Trigger-target list consumed by tests/test_auth_access_sync.py:
-- (table_name, id_column, owner_column)
-- ('agent_definitions', 'id', 'owner_user_id')    -- from migration 123
-- ('hook_definitions', 'id', 'owner_user_id')     -- from migration 123
-- ('memories', 'id', 'user_id')                   -- from migration 123
-- ('mcp_server_configs', 'id', 'owner_user_id')   -- from migration 123
-- ('models', 'id', 'owner_user_id')               -- from migration 123
-- ('projects', 'id', 'user_id')                   -- from migration 123
-- ('sandboxes', 'id', 'created_by')               -- from migration 123
-- ('sandbox_templates', 'id', 'owner_user_id')    -- from migration 123
-- ('schedules', 'id', 'created_by')               -- from migration 123
-- ('secrets', 'id', 'owner_user_id')              -- from migration 123
-- ('skill_definitions', 'id', 'owner_user_id')    -- from migration 123
-- ('managed_tool_definitions', 'id', 'owner_user_id') -- this migration

-- ---------------------------------------------------------------------------
-- 3. Group ownership: share with the owning group, like secrets (124).
--
-- Migration 123 only files clearances for *user* owners. Group-owned
-- definitions (owner_user_id IS NULL, owner_group_id set) need a read
-- clearance for the group principal — same trigger function (and the same
-- owner column names) as migration 124's secrets triggers.
-- ---------------------------------------------------------------------------

CREATE TRIGGER auth_group_clearance
AFTER INSERT ON public.agent_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_group_clearance_for_row();

CREATE TRIGGER auth_group_clearance
AFTER INSERT ON public.skill_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_group_clearance_for_row();

CREATE TRIGGER auth_group_clearance
AFTER INSERT ON public.mcp_server_configs
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_group_clearance_for_row();

CREATE TRIGGER auth_group_clearance
AFTER INSERT ON public.hook_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_group_clearance_for_row();

CREATE TRIGGER auth_group_clearance
AFTER INSERT ON public.managed_tool_definitions
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_group_clearance_for_row();

-- Backfill: existing group-owned definitions. Only fires the 124-style
-- clearance where the legacy group-owner predicate fired: user owner unset.
INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id)
SELECT t.auth_id, 'read', 'group', t.owner_group_id
FROM public.agent_definitions t
WHERE t.owner_group_id IS NOT NULL AND t.owner_user_id IS NULL AND t.auth_id IS NOT NULL
UNION ALL
SELECT t.auth_id, 'read', 'group', t.owner_group_id
FROM public.skill_definitions t
WHERE t.owner_group_id IS NOT NULL AND t.owner_user_id IS NULL AND t.auth_id IS NOT NULL
UNION ALL
SELECT t.auth_id, 'read', 'group', t.owner_group_id
FROM public.mcp_server_configs t
WHERE t.owner_group_id IS NOT NULL AND t.owner_user_id IS NULL AND t.auth_id IS NOT NULL
UNION ALL
SELECT t.auth_id, 'read', 'group', t.owner_group_id
FROM public.hook_definitions t
WHERE t.owner_group_id IS NOT NULL AND t.owner_user_id IS NULL AND t.auth_id IS NOT NULL
UNION ALL
SELECT t.auth_id, 'read', 'group', t.owner_group_id
FROM public.managed_tool_definitions t
WHERE t.owner_group_id IS NOT NULL AND t.owner_user_id IS NULL AND t.auth_id IS NOT NULL
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. Backfill: built-ins get their org-wide read grant.
-- ---------------------------------------------------------------------------

INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id)
SELECT t.auth_id, 'read', 'org', t.organization_id
FROM public.agent_definitions t
WHERE t.scope = 'built-in' AND t.organization_id IS NOT NULL AND t.auth_id IS NOT NULL
UNION ALL
SELECT t.auth_id, 'read', 'org', t.organization_id
FROM public.skill_definitions t
WHERE t.scope = 'built-in' AND t.organization_id IS NOT NULL AND t.auth_id IS NOT NULL
UNION ALL
SELECT t.auth_id, 'read', 'org', t.organization_id
FROM public.mcp_server_configs t
WHERE t.scope = 'built-in' AND t.organization_id IS NOT NULL AND t.auth_id IS NOT NULL
UNION ALL
SELECT t.auth_id, 'read', 'org', t.organization_id
FROM public.hook_definitions t
WHERE t.scope = 'built-in' AND t.organization_id IS NOT NULL AND t.auth_id IS NOT NULL
UNION ALL
SELECT t.auth_id, 'read', 'org', t.organization_id
FROM public.managed_tool_definitions t
WHERE t.scope = 'built-in' AND t.organization_id IS NOT NULL AND t.auth_id IS NOT NULL
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

-- ---------------------------------------------------------------------------
-- 5. Orphaned instance rows: attach them to the org's owner user. The 123
-- UPDATE-transfer trigger then files their owner clearance; for
-- managed_tool_definitions the trigger added in section 2 does the same once
-- it exists (created before this UPDATE runs).
-- ---------------------------------------------------------------------------

UPDATE agent_definitions t
SET owner_user_id = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id IS NULL
  AND t.organization_id = org.organization_id;

UPDATE skill_definitions t
SET owner_user_id = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id IS NULL
  AND t.organization_id = org.organization_id;

UPDATE mcp_server_configs t
SET owner_user_id = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id IS NULL
  AND t.organization_id = org.organization_id;

UPDATE hook_definitions t
SET owner_user_id = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id IS NULL
  AND t.organization_id = org.organization_id;

UPDATE managed_tool_definitions t
SET owner_user_id = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id IS NULL
  AND t.organization_id = org.organization_id;