-- Rollback for 114_tenant_roles: restore ownership to the bootstrap role and
-- remove the tenant roles.
--
-- lucent:rollback=irreversible
--
-- WHY IRREVERSIBLE-CLASS: this rollback reassigns ownership INTO the
-- bootstrap superuser and drops roles the runner session (lucent_app after
-- cutover) cannot legally touch — REASSIGN OWNED requires membership in both
-- source and target roles, and DROP ROLE requires zero held privileges. Run
-- it manually as a superuser (psql as the bootstrap role) if the wave must
-- be undone; the checksummed runner will refuse it as lucent_app, which is
-- the correct fail-closed behavior for a tenant-isolation rollback.
--
-- Order matters: reverse the db_snapshot re-home, strip default-privilege
-- entries and object ACLs (DROP ROLE refuses a role that holds any
-- privileges), move owned objects, then drop the roles.

-- Reverse the db_snapshot re-home: the tool's pg_dump user env var returns
-- to the in-source default (lucent_daemon). Guarded on the exact value this
-- migration set so a user-customized env_vars row is never clobbered.
UPDATE managed_tool_definitions
SET env_vars = '{}'::jsonb
WHERE name = 'db_snapshot'
  AND env_vars = '{"LUCENT_DB_USER": "lucent_backup"}'::jsonb;

-- Drop the default-privilege entries 114 created (they keep DROP ROLE
-- failing even after object ACLs are revoked).
ALTER DEFAULT PRIVILEGES FOR ROLE lucent_app IN SCHEMA public
    REVOKE ALL ON TABLES FROM lucent_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE lucent_app IN SCHEMA public
    REVOKE ALL ON TABLES FROM lucent_diagnostics;
ALTER DEFAULT PRIVILEGES FOR ROLE lucent_app IN SCHEMA public
    REVOKE ALL ON TABLES FROM lucent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE lucent_app IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM lucent_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM lucent_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM lucent_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM lucent_diagnostics;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM lucent_backup;

-- Object ACLs held by the wave roles.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM lucent_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM lucent_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM lucent_diagnostics;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM lucent_backup;
REVOKE USAGE ON SCHEMA public FROM lucent_app;
REVOKE USAGE ON SCHEMA public FROM lucent_diagnostics;
REVOKE USAGE ON SCHEMA public FROM lucent_backup;

-- Move owned objects back to the bootstrap role, then clear the rest.
REASSIGN OWNED BY lucent_app TO lucent;
DROP OWNED BY lucent_app;
DROP OWNED BY lucent_diagnostics;
DROP OWNED BY lucent_backup;

DROP ROLE IF EXISTS lucent_backup;
DROP ROLE IF EXISTS lucent_diagnostics;
DROP ROLE IF EXISTS lucent_app;