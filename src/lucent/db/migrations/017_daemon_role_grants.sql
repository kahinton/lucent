-- Migration 017: Least-privilege grants for the daemon DB role
-- The lucent_daemon role is created in docker/init.sql (Docker) or manually.
-- This migration grants only what the daemon needs: read users/orgs to find
-- its service account, and full CRUD on api_keys to manage its own keys.
-- If the role doesn't exist, the migration is a safe no-op.

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
    -- Daemon needs to look up its service account and organization
    EXECUTE 'GRANT SELECT ON users TO lucent_daemon';
    EXECUTE 'GRANT SELECT ON organizations TO lucent_daemon';

    -- Daemon needs full lifecycle control over its own API keys
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON api_keys TO lucent_daemon';

    -- Ensure the daemon service application user exists so the daemon
    -- role never needs INSERT on users.
    -- Pick the first organization; if none exists yet the daemon will
    -- still work (organization_id is nullable on users).
    INSERT INTO users (external_id, provider, email, display_name, role, organization_id)
    VALUES (
      'daemon-service', 'local', 'daemon@lucent.local', 'Lucent Daemon', 'member',
      (SELECT id FROM organizations ORDER BY created_at LIMIT 1)
    )
    ON CONFLICT (provider, external_id) DO NOTHING;

    RAISE NOTICE 'Granted daemon role privileges on users, organizations, api_keys';
  ELSE
    RAISE NOTICE 'lucent_daemon role not found — skipping grants (non-Docker setup)';
  END IF;
END $$;
