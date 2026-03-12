-- PostgreSQL initialization script for Docker
-- This runs when the container is first created

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Set default similarity threshold for fuzzy matching
ALTER DATABASE lucent SET pg_trgm.similarity_threshold = 0.3;

-- Create a restricted role for daemon API key provisioning.
-- This role has NO access to memories, audit logs, or other application data.
-- Actual table-level grants are applied by migration 017 after tables exist.
-- In production, override the password via: ALTER ROLE lucent_daemon PASSWORD 'your-secret';
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
    CREATE ROLE lucent_daemon WITH LOGIN PASSWORD 'lucent_daemon_dev_password';
  END IF;
END $$;

GRANT CONNECT ON DATABASE lucent TO lucent_daemon;
GRANT USAGE ON SCHEMA public TO lucent_daemon;
