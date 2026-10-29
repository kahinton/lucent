-- Down migration for 160: collapse origin back into scope.
--
-- Operator-provisioned rows must not survive as ordinary `scope='built-in'` rows
-- on a downgraded binary: the built-in sync would then treat them as its own and
-- could overwrite or archive them (and a name it does not recognise would be
-- pruned). Archiving is the honest rollback — the definition, its grants, and its
-- run history remain readable for audit, and `status != 'active'` already blocks
-- execution — so nothing is silently deleted.

UPDATE mcp_server_configs
SET status = 'archived', updated_at = NOW()
WHERE origin = 'operator' AND status <> 'archived';

UPDATE managed_tool_definitions
SET status = 'archived', updated_at = NOW()
WHERE origin = 'operator' AND status <> 'archived';

UPDATE skill_definitions
SET status = 'archived', updated_at = NOW()
WHERE origin = 'operator' AND status <> 'archived';

UPDATE agent_definitions
SET status = 'archived', updated_at = NOW()
WHERE origin = 'operator' AND status <> 'archived';

DROP INDEX IF EXISTS idx_mcp_cfg_origin;
DROP INDEX IF EXISTS idx_managed_tool_defs_origin;
DROP INDEX IF EXISTS idx_skill_defs_origin;
DROP INDEX IF EXISTS idx_agent_defs_origin;

ALTER TABLE mcp_server_configs DROP CONSTRAINT IF EXISTS ck_mcp_cfg_origin;
ALTER TABLE managed_tool_definitions DROP CONSTRAINT IF EXISTS ck_managed_tool_origin;
ALTER TABLE skill_definitions DROP CONSTRAINT IF EXISTS ck_skill_def_origin;
ALTER TABLE agent_definitions DROP CONSTRAINT IF EXISTS ck_agent_def_origin;

ALTER TABLE mcp_server_configs DROP COLUMN IF EXISTS origin;
ALTER TABLE managed_tool_definitions DROP COLUMN IF EXISTS origin;
ALTER TABLE skill_definitions DROP COLUMN IF EXISTS origin;
ALTER TABLE agent_definitions DROP COLUMN IF EXISTS origin;
