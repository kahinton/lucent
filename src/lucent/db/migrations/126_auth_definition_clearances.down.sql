-- Undo migration 126. Order matters: reset the orphaned owners *before*
-- dropping the transfer triggers so their owner clearances are removed
-- automatically, then drop the triggers and the org-wide read grants.

UPDATE agent_definitions t
SET owner_user_id = NULL
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NOT NULL
  AND t.owner_group_id IS NULL
  AND t.owner_user_id IN (
      SELECT u.id FROM users u WHERE u.role = 'owner' AND u.organization_id = t.organization_id
  );

UPDATE skill_definitions t
SET owner_user_id = NULL
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NOT NULL
  AND t.owner_group_id IS NULL
  AND t.owner_user_id IN (
      SELECT u.id FROM users u WHERE u.role = 'owner' AND u.organization_id = t.organization_id
  );

UPDATE mcp_server_configs t
SET owner_user_id = NULL
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NOT NULL
  AND t.owner_group_id IS NULL
  AND t.owner_user_id IN (
      SELECT u.id FROM users u WHERE u.role = 'owner' AND u.organization_id = t.organization_id
  );

UPDATE hook_definitions t
SET owner_user_id = NULL
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NOT NULL
  AND t.owner_group_id IS NULL
  AND t.owner_user_id IN (
      SELECT u.id FROM users u WHERE u.role = 'owner' AND u.organization_id = t.organization_id
  );

UPDATE managed_tool_definitions t
SET owner_user_id = NULL
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NOT NULL
  AND t.owner_group_id IS NULL
  AND t.owner_user_id IN (
      SELECT u.id FROM users u WHERE u.role = 'owner' AND u.organization_id = t.organization_id
  );

DROP TRIGGER IF EXISTS auth_builtin_org_read ON public.agent_definitions;
DROP TRIGGER IF EXISTS auth_builtin_org_read ON public.skill_definitions;
DROP TRIGGER IF EXISTS auth_builtin_org_read ON public.mcp_server_configs;
DROP TRIGGER IF EXISTS auth_builtin_org_read ON public.hook_definitions;
DROP TRIGGER IF EXISTS auth_builtin_org_read ON public.managed_tool_definitions;
DROP TRIGGER IF EXISTS auth_group_clearance ON public.agent_definitions;
DROP TRIGGER IF EXISTS auth_group_clearance ON public.skill_definitions;
DROP TRIGGER IF EXISTS auth_group_clearance ON public.mcp_server_configs;
DROP TRIGGER IF EXISTS auth_group_clearance ON public.hook_definitions;
DROP TRIGGER IF EXISTS auth_group_clearance ON public.managed_tool_definitions;
DROP TRIGGER IF EXISTS auth_owner_clearance ON public.managed_tool_definitions;
DROP TRIGGER IF EXISTS auth_owner_clearance_update ON public.managed_tool_definitions;
DROP FUNCTION IF EXISTS public.grant_auth_org_read_for_builtin();

DELETE FROM auth_clearances
WHERE principal_type = 'org'
  AND role = 'read'
  AND auth_id IN (
      SELECT auth_id FROM public.agent_definitions
      UNION SELECT auth_id FROM public.skill_definitions
      UNION SELECT auth_id FROM public.mcp_server_configs
      UNION SELECT auth_id FROM public.hook_definitions
      UNION SELECT auth_id FROM public.managed_tool_definitions
  );

-- The group clearances mirror migration 124's secrets semantics exactly (read
-- clearances for the owning group on rows without a user owner) — same shape
-- that migration's down removes for secrets:
DELETE FROM auth_clearances c
USING public.agent_definitions t
WHERE c.auth_id = t.auth_id
  AND c.principal_type = 'group'
  AND c.role = 'read'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id = c.principal_id;

DELETE FROM auth_clearances c
USING public.skill_definitions t
WHERE c.auth_id = t.auth_id
  AND c.principal_type = 'group'
  AND c.role = 'read'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id = c.principal_id;

DELETE FROM auth_clearances c
USING public.mcp_server_configs t
WHERE c.auth_id = t.auth_id
  AND c.principal_type = 'group'
  AND c.role = 'read'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id = c.principal_id;

DELETE FROM auth_clearances c
USING public.hook_definitions t
WHERE c.auth_id = t.auth_id
  AND c.principal_type = 'group'
  AND c.role = 'read'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id = c.principal_id;

DELETE FROM auth_clearances c
USING public.managed_tool_definitions t
WHERE c.auth_id = t.auth_id
  AND c.principal_type = 'group'
  AND c.role = 'read'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id = c.principal_id;