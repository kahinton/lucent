-- Down migration for 147: remove per-user capability consent.
--
-- Dropping the consent rows is intentional and destructive: consent is a
-- security decision, and there is no earlier schema that can represent it. A
-- rollback returns the instance to "attached + readable means available".

DROP TABLE IF EXISTS agent_capability_consents;

ALTER TABLE agent_skills DROP COLUMN IF EXISTS granted_by;
ALTER TABLE agent_managed_tools DROP COLUMN IF EXISTS granted_by;
ALTER TABLE agent_hooks DROP COLUMN IF EXISTS granted_by;
ALTER TABLE agent_mcp_servers DROP COLUMN IF EXISTS granted_by;
