-- Down migration for 159: drop the per-user credential slot declaration.
--
-- The column is metadata only: no authorization decision depends on it, and the
-- `secret://` references in `headers` keep resolving per user without it. Dropping
-- it therefore degrades the "add your token" prompt to a plain failure and changes
-- nothing about what a caller may reach.

DROP INDEX IF EXISTS idx_mcp_cfg_builtin;

ALTER TABLE mcp_server_configs
    DROP COLUMN IF EXISTS credential_slots;
