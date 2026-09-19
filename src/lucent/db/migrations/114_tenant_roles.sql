-- Migration 114: tenant-isolation roles (prerequisite for RLS in 116).
--
-- Implements the 9/10 enforcement-mechanism evaluation §6 phase plan
-- (originally numbered 111/112; 111-115 were consumed by the projects
-- feature, handoff-privacy hardening, and the orphan-table provisioning,
-- so this wave is 114/115/116).
--
-- The server pool connects as `lucent` (superuser, BYPASSRLS) today, so
-- RLS cannot bind it. This migration creates the application, diagnostics,
-- and backup roles and their grants, and reassigns table ownership to the
-- app role so future ALTER TABLE migrations (which run through the server
-- pool) keep working after the compose DATABASE_URL switch.
--
-- Role contract:
--   lucent_app         : the server pool role after cutover. DML on all
--                        tables + DDL on the schema (migration runner). NOT
--                        superuser, NOT BYPASSRLS -> RLS policies bind it.
--   lucent_diagnostics : db_query transport. SELECT-only; row visibility
--                        comes from the RLS policies (116).
--   lucent_backup      : db_snapshot transport. SELECT-only + BYPASSRLS so
--                        pg_dump can never silently truncate to
--                        policy-visible rows.
--   lucent_daemon      : pre-existing least-privilege role used by the
--                        daemon's direct connects; org-wide row visibility
--                        comes from the RLS daemon policy branch (116).
--   lucent             : stays the bootstrap/superuser role but is no
--                        longer the application connection role. Login is
--                        NOT disabled here (keep the emergency path while
--                        cutover is manual); disable login after cutover
--                        is verified.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lucent_app') THEN
        CREATE ROLE lucent_app LOGIN;  -- password set out-of-band (compose secret)
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lucent_diagnostics') THEN
        CREATE ROLE lucent_diagnostics LOGIN;  -- password set out-of-band
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lucent_backup') THEN
        CREATE ROLE lucent_backup LOGIN BYPASSRLS;  -- snapshots only
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- lucent_app: full DML + DDL (the server runs the checksummed migration
-- runner through this pool, so CREATE on schema public is required).
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO lucent_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lucent_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lucent_app;
GRANT CREATE ON SCHEMA public TO lucent_app;
-- Default privileges are keyed to the creating role. After cutover every
-- migration creates objects as lucent_app, so the FOR ROLE clauses below are
-- what keeps the other transports reading future tables. The unconditional
-- clauses keep today's (lucent-owned) family covered.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lucent_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO lucent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE lucent_app IN SCHEMA public
    GRANT SELECT ON TABLES TO lucent_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE lucent_app IN SCHEMA public
    GRANT SELECT ON TABLES TO lucent_diagnostics;
ALTER DEFAULT PRIVILEGES FOR ROLE lucent_app IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lucent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE lucent_app IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO lucent_app;

-- ---------------------------------------------------------------------------
-- lucent_diagnostics: read-only; the RLS policies decide which rows.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO lucent_diagnostics;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO lucent_diagnostics;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO lucent_diagnostics;

-- ---------------------------------------------------------------------------
-- lucent_backup: full read for pg_dump (bypasses RLS by design).
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO lucent_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO lucent_backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO lucent_backup;

-- Re-home db_snapshot off the RLS-bound role: the tool execs pg_dump inside
-- the postgres container as the user named by its LUCENT_DB_USER env var
-- (in-source default: lucent_daemon). Under 116's FORCE RLS a context-less
-- lucent_daemon session sees zero tenant rows, so pg_dump would silently
-- truncate every dump taken after cutover. This statement points the tool
-- row's env_vars at lucent_backup (BYPASSRLS above), making the re-home a
-- property of the migration wave instead of an out-of-band compose edit.
-- No-op on fresh installs (no such row). Restore stays untouched: it is a
-- manual Kyle-side path and was already non-owner before this wave.
UPDATE managed_tool_definitions
SET env_vars = '{"LUCENT_DB_USER": "lucent_backup"}'::jsonb
WHERE name = 'db_snapshot'
  AND env_vars = '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- Ownership: the projects feature and every prior migration created tables
-- owned by `lucent`. Under FORCE RLS the owner is bound by policy (that is
-- the point), but ownership also gates ALTER TABLE / CREATE POLICY. Migrate
-- ownership to lucent_app so the migration runner keeps working after the
-- DATABASE_URL switch, while `lucent` remains a break-glass superuser.
--
-- WHY NOT REASSIGN OWNED: `REASSIGN OWNED BY lucent TO lucent_app` aborts
-- on the pinned shared objects every initdb grants the bootstrap role
-- (databases incl. template0/template1, tablespaces pg_default/pg_global) —
-- live-verified 2026-09-14: "cannot reassign ownership of objects owned by
-- role lucent because they are required by the database system". On any
-- compose stack the superuser is also the cluster bootstrap, so REASSIGN
-- can never complete here. Transfer per kind instead, scoped to schema
-- public (the surface the app and the migration runner touch). Pinned
-- bootstrap objects, extension-owned functions/types/operators, and other
-- public types/operators stay with the break-glass superuser on purpose;
-- no repo migration path exercises ALTER TYPE / ALTER OPERATOR.
-- Run before 116 so FORCE RLS never applies to a table owned by an
-- ungrantable role.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    obj record;
BEGIN
    -- Relations: tables (incl. partitioned roots), sequences, views,
    -- matviews currently owned by the bootstrap role.
    FOR obj IN
        SELECT c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'S', 'v', 'm')
          AND pg_get_userbyid(c.relowner) = 'lucent'
    LOOP
        IF obj.relkind IN ('r', 'p') THEN
            EXECUTE format('ALTER TABLE %I OWNER TO lucent_app', obj.relname);
        ELSIF obj.relkind = 'S' THEN
            EXECUTE format('ALTER SEQUENCE %I OWNER TO lucent_app', obj.relname);
        ELSIF obj.relkind = 'm' THEN
            EXECUTE format('ALTER MATERIALIZED VIEW %I OWNER TO lucent_app', obj.relname);
        ELSE
            EXECUTE format('ALTER VIEW %I OWNER TO lucent_app', obj.relname);
        END IF;
    END LOOP;

    -- Functions: CREATE OR REPLACE post-cutover needs ownership. Exclude
    -- extension-owned routines (pg_depend deptype='e') — Postgres forbids
    -- transferring them and nothing in the app alters them.
    FOR obj IN
        SELECT p.proname,
               pg_get_function_identity_arguments(p.oid) AS args
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND pg_get_userbyid(p.proowner) = 'lucent'
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_proc'::regclass
                AND d.objid = p.oid
                AND d.deptype = 'e'
          )
    LOOP
        EXECUTE format('ALTER FUNCTION %I(%s) OWNER TO lucent_app',
                       obj.proname, obj.args);
    END LOOP;
END
$$;

-- The legacy role keeps superuser for bootstrap but is no longer the app
-- connection role; it keeps only the pinned bootstrap objects (system
-- databases/tablespaces) and extension-owned artifacts above.
COMMENT ON ROLE lucent_app IS
    'Application connection role (server pool). Non-superuser, no BYPASSRLS: RLS policies bind it. Created by migration 114 for tenant isolation.';
COMMENT ON ROLE lucent_diagnostics IS
    'db_query transport role: SELECT-only; row visibility via RLS policies. Created by migration 114.';
COMMENT ON ROLE lucent_backup IS
    'db_snapshot transport role: SELECT-only + BYPASSRLS so full-database backups never truncate to policy-visible rows. Created by migration 114.';