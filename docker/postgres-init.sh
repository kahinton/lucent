#!/bin/sh
set -eu

# PostgreSQL initialization script for Docker. This runs only when the database
# volume is first created.

: "${POSTGRES_DB:=lucent}"
: "${POSTGRES_USER:=lucent}"
: "${DAEMON_DB_PASSWORD:?DAEMON_DB_PASSWORD is required for lucent_daemon role setup}"

psql \
  -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=db_name="$POSTGRES_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

ALTER DATABASE :"db_name" SET pg_trgm.similarity_threshold = 0.3;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
    CREATE ROLE lucent_daemon WITH LOGIN;
  END IF;
END $$;
SQL

psql \
  -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=db_name="$POSTGRES_DB" \
  --set=daemon_password="$DAEMON_DB_PASSWORD" <<'SQL'
ALTER ROLE lucent_daemon WITH PASSWORD :'daemon_password';
GRANT CONNECT ON DATABASE :"db_name" TO lucent_daemon;
GRANT USAGE ON SCHEMA public TO lucent_daemon;
SQL