#!/usr/bin/env bash
# Creates and starts the disposable PostgreSQL container the test suite's
# integration tests (`pytest -m integration`) run against.
#
# This environment is completely separate from the application database
# (container `lucent-db`, port 5433): its own container, its own credentials,
# its own port, and its own data volume. Nothing the tests do here can touch
# the live database — tests/conftest.py additionally refuses any test URL
# aimed at the real one.
#
# The cluster is prepared to mirror the production Docker setup, which the
# migrations assume:
#   - docker/postgres-init.sh runs at first-init (via initdb.d) — it creates
#     the `lucent_daemon` role and the uuid-ossp/pg_trgm extensions, which
#     migrations 097/098/102 grant to unconditionally.
#   - an inert, login-less `lucent` role is created (migration 090 does
#     ALTER DEFAULT PRIVILEGES FOR ROLE lucent). The test cluster's real
#     bootstrap user is `lucent_test`; this shell role exists only here.
#
# The schema itself is NOT baked in: db_pool's first connection applies the
# full migration set automatically (init_db runs migrations), so the
# container only needs to exist, be empty, and have those roles ready.
#
# Unit tests (`pytest`) never connect to any database, so this script is only
# needed to run the integration suite.
#
# Usage:
#   scripts/dev-test-db.sh            start or create the test database
#   scripts/dev-test-db.sh --fresh    wipe it and start over (disposable)
set -euo pipefail

CONTAINER=lucent-test-db
PORT=5434
DB_USER=lucent_test
DB_PASSWORD=lucent-test-only
DB_NAME=lucent_test
IMAGE=postgres:16-alpine
VOLUME=lucent-test-db-data

case "${1:-}" in
  --fresh)
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
    echo "wiped $CONTAINER and $VOLUME"
    ;;
  ""|start)
    ;;
  *)
    echo "usage: scripts/dev-test-db.sh [--fresh]" >&2
    exit 2
    ;;
esac

if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  repo_root="$(cd "$(dirname "$0")/.." && pwd)"
  docker run -d --name "$CONTAINER" \
    -e POSTGRES_USER="$DB_USER" \
    -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -e POSTGRES_DB="$DB_NAME" \
    -e DAEMON_DB_PASSWORD="$DB_PASSWORD" \
    -p "${PORT}:5432" \
    -v "${VOLUME}:/var/lib/postgresql/data" \
    -v "${repo_root}/docker/postgres-init.sh:/docker-entrypoint-initdb.d/10-lucent-init.sh:ro" \
    "$IMAGE" >/dev/null
  echo "created $CONTAINER ($IMAGE, host port $PORT, data volume $VOLUME)"
fi

docker start "$CONTAINER" >/dev/null

# Wait until the REAL server accepts connections. Probe over TCP from inside
# the container on purpose: during first initialization postgres runs through
# a transient, local-socket-only temp server (that is how the initdb.d
# scripts run), and a unix-socket probe would report ready mid-init — racing
# the very scripts this script depends on. The temp server never listens on
# TCP, so it cannot pass this probe.
ready=0
for _ in $(seq 1 240); do
  if docker exec "$CONTAINER" pg_isready -h 127.0.0.1 -p 5432 -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  echo "timed out waiting for $CONTAINER to accept connections" >&2
  exit 1
fi

# Migrations name the application owner role ('lucent' — the POSTGRES_USER in
# production, e.g. 090's ALTER DEFAULT PRIVILEGES FOR ROLE lucent). The test
# cluster's bootstrap user is $DB_USER, so create an inert (NOLOGIN) shell of
# that role here; only the test tooling creates it, only this test DB has it.
# Retry while the server settles, then VERIFY so this script cannot claim
# success without it.
for _ in $(seq 1 30); do
  if docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -q <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent') THEN
    EXECUTE 'CREATE ROLE lucent NOLOGIN';
  END IF;
END $$;
SQL
  then
    break
  fi
  sleep 2
done
roles_present="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
  "SELECT count(*) FROM pg_roles WHERE rolname IN ('lucent', 'lucent_daemon')")"
if [ "$roles_present" != "2" ]; then
  echo "expected roles lucent + lucent_daemon; found $roles_present" >&2
  exit 1
fi

echo "ready: postgresql://${DB_USER}:${DB_PASSWORD}@localhost:${PORT}/${DB_NAME}"