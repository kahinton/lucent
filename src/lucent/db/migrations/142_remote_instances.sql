-- Migration 142: Remote instances (Agent Control Protocol)
--
-- A "remote" is a non-local work executor: another Lucent deployment, or any
-- peer running the shipped stdlib remote agent client. Lucent (the controller)
-- pins whole tasks to a remote via ``tasks.remote_target_id``; the remote
-- polls the controller over HTTPS, claims its targeted tasks with a scoped
-- ``lr_`` API key, executes them, and reports events/results back over the
-- same outbound-only connection. The remote never opens inbound ports.
--
-- Security posture:
-- - No credential material is stored here. Keys live in ``api_keys`` (bcrypt
--   hashed) owned by a per-remote identity user ``remote-agent:{remote_id}``.
-- - Owner-only registration/management; admin read-only.
-- - Every remote also registers as a ``daemon_instances`` row
--   (``remote:{remote_id}``) at claim time, so lease renewal and the stale
--   task reaper apply to remote claims with no special casing.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'remote_instances'
          AND c.relkind = 'r'
          AND n.nspname = current_schema()
    ) THEN
        EXECUTE '
            CREATE TABLE remote_instances (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                name            VARCHAR(128) NOT NULL,
                description     TEXT,
                -- active: usable for dispatch; disabled: pinned tasks queue but
                -- cannot be claimed; revoked: terminal, key invalidated.
                status          VARCHAR(16) NOT NULL DEFAULT ''active''
                    CHECK (status IN (''active'', ''disabled'', ''revoked'')),
                -- Forward-compatible capability advertisement, e.g.
                -- {"executors": ["command"], "max_output_bytes": 1048576}
                capabilities    JSONB NOT NULL DEFAULT ''{}''::jsonb,
                last_seen_at    TIMESTAMPTZ,
                created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (organization_id, name)
            )';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_remote_instances_org_status
    ON remote_instances (organization_id, status);

-- Targeting pin: mirrors tasks.backend_id, but for out-of-process executors.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS remote_target_id UUID REFERENCES remote_instances(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_remote_target
    ON tasks (remote_target_id)
    WHERE remote_target_id IS NOT NULL;