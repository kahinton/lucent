-- Migration 148: in-flight model output for the activity page.
--
-- The request detail page shows what a running task's model is *currently*
-- writing. The daemon that owns the session runs in a separate process (and
-- often a separate container) from the web server, so the text has to cross a
-- process boundary. The existing `tasks.result` column is only written when a
-- task settles, which is why the page could previously only show "Running"
-- with no content until the very end.
--
-- One row per task, overwritten in place (never appended to history): this is
-- scratch state, not an audit trail. The daemon deletes the row when the task
-- settles, at which point `tasks.result` becomes the durable source of truth.
--
-- Deliberately NOT mirrored into `task_events`:
--   * `task_events` is the durable timeline the activity feed and validation
--     logic read; writing a row per stream tick would flood it and would make
--     the historical record meaningless.
--   * No NOTIFY trigger either. Migration 104's triggers make the browser
--     re-fetch whole pages on task/request mutations; firing that every flush
--     would turn a streaming task into a page-reload loop. The page polls a
--     small read-only endpoint instead.
--
-- `content` is bounded on write (the daemon keeps the tail), so a runaway model
-- cannot grow the row without limit.

CREATE TABLE IF NOT EXISTS task_live_output (
    task_id UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    content TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reads are always "these task ids in this org", served by the primary key.
-- The org index exists so an organization-wide cleanup can find orphans left
-- behind by a daemon crash without a sequential scan.
CREATE INDEX IF NOT EXISTS idx_task_live_output_org
    ON task_live_output(organization_id, updated_at DESC);

-- Same least-privilege pattern as the other daemon-written tables: data-plane
-- CRUD only, never DDL (the server owns migrations).
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON task_live_output TO lucent_daemon';
    END IF;
END $$;
