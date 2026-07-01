-- Migration 089: Re-apply daemon SELECT grant on runtime_settings.
-- Migration 086 created runtime_settings and granted SELECT to lucent_daemon,
-- but that grant is conditional on the role existing at apply-time. On instances
-- where the lucent_daemon role was created or restored after 086 was recorded as
-- applied (e.g. dump/restore cycles), the grant never took, so the daemon's
-- restricted role hits "permission denied for table runtime_settings" and
-- silently falls back to env/defaults instead of reading DB-managed settings.
-- This migration re-applies the grant idempotently so the daemon reads the new
-- runtime_settings table on every deployment. Safe no-op if the role is absent.

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
    EXECUTE 'GRANT SELECT ON runtime_settings TO lucent_daemon';
    RAISE NOTICE 'Granted daemon role SELECT on runtime_settings';
  ELSE
    RAISE NOTICE 'lucent_daemon role not found — skipping runtime_settings grant';
  END IF;
END $$;
