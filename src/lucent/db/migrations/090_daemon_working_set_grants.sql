-- Migration 090: Codify the daemon role's working-set privileges.
--
-- Background: migration 017 set up a minimal grant set for lucent_daemon
-- (users/organizations SELECT, api_keys CRUD) from when the daemon only
-- provisioned its own API keys. Since then the daemon became the autonomous
-- worker: it reads and writes schedules, tasks, requests, memories, llm_sessions,
-- definitions, the model registry, and more via its direct lucent_daemon
-- connection. Those grants were never codified in a migration — they only ever
-- existed on instances where someone applied them by hand. A fresh deployment
-- (docker-compose up with a clean volume) gets just migration 017's minimal set,
-- so the daemon fails on a clean install:
--   * pool init -> "permission denied for schema public" (tried to run migrations)
--   * cannot seed system schedules (no grant on schedules/tasks)
--   * cannot load the model registry (no grant on models)
--   * falls back to hardcoded engine/model defaults (copilot / gpt-4.1)
--
-- This migration grants lucent_daemon the data-plane privileges it actually
-- needs — CRUD on all current and future tables in schema public, plus sequence
-- usage — while deliberately WITHHOLDING DDL. The role is NOT granted CREATE on
-- schema public, so it still cannot run migrations or alter the schema. That one
-- boundary is what keeps "the server owns migrations" true (the daemon now passes
-- run_migrations=False to init_db). Cross-tenant isolation is enforced at the
-- API-key/application layer, not by this single service role.
--
-- Runs as the migration user (lucent), which owns the schema, so the grants and
-- default privileges take effect. Idempotent; safe no-op if the role is absent.

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
    -- Schema access (no CREATE — the daemon must never perform DDL/migrations).
    EXECUTE 'GRANT USAGE ON SCHEMA public TO lucent_daemon';

    -- CRUD on every existing table in the schema.
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lucent_daemon';

    -- Sequence usage for any SERIAL/identity columns the daemon writes.
    EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lucent_daemon';

    -- Auto-grant the same privileges on tables/sequences created by future
    -- migrations (which run as the lucent role), so this never drifts again.
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE lucent IN SCHEMA public '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lucent_daemon';
    EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE lucent IN SCHEMA public '
            'GRANT USAGE, SELECT ON SEQUENCES TO lucent_daemon';

    RAISE NOTICE 'Granted daemon role data-plane CRUD (no DDL) on schema public';
  ELSE
    RAISE NOTICE 'lucent_daemon role not found — skipping daemon working-set grants';
  END IF;
END $$;
