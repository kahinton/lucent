-- Migration 054: Multi-instance task distribution
-- Adds daemon instance registry + task lease fields for robust multi-daemon coordination.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'daemon_instances'
          AND c.relkind = 'r'
          AND n.nspname = current_schema()
    ) THEN
        -- Defensive cleanup for environments where a stale composite type exists
        -- under this name without the backing relation.
        IF EXISTS (
            SELECT 1
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE t.typname = 'daemon_instances'
              AND n.nspname = current_schema()
        ) THEN
            EXECUTE 'DROP TYPE daemon_instances';
        END IF;

        EXECUTE '
            CREATE TABLE daemon_instances (
                instance_id VARCHAR(128) NOT NULL,
                organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                hostname VARCHAR(255),
                pid INTEGER,
                roles TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                status VARCHAR(16) NOT NULL DEFAULT ''active'',
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                metadata JSONB NOT NULL DEFAULT ''{}''::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (instance_id, organization_id)
            )';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_daemon_instances_org_status_seen
    ON daemon_instances (organization_id, status, last_seen_at DESC);

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_version INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_tasks_claim_expires_at
    ON tasks (claim_expires_at)
    WHERE status IN ('claimed', 'running');

-- Backfill existing claimed/running tasks with a best-effort lease baseline.
UPDATE tasks
SET
    last_heartbeat_at = COALESCE(last_heartbeat_at, claimed_at),
    claim_expires_at = COALESCE(claim_expires_at, claimed_at + INTERVAL '30 minutes')
WHERE
    status IN ('claimed', 'running')
    AND claimed_at IS NOT NULL;
