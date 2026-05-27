-- Migration 018: Event-driven task dispatch via PG NOTIFY
--
-- Adds trigger functions that fire pg_notify('task_ready', ...) when:
--   1. A task is created with pending/planned status (immediate dispatch)
--   2. A task completes/fails/is cancelled (may unblock downstream tasks)
--   3. A task is released back to pending (stale recovery)
--
-- LISTEN/NOTIFY is transient — if no daemon is listening, notifications are
-- silently dropped.  The daemon's dispatch loop polls as a fallback.
-- Multiple daemon instances can LISTEN on the same channel safely.


-- Trigger function: emit a JSON payload on the 'task_ready' channel.
-- TG_ARGV[0] carries the event type (created, completed, released).
CREATE OR REPLACE FUNCTION notify_task_ready()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'task_ready',
        jsonb_build_object(
            'task_id', NEW.id,
            'request_id', NEW.request_id,
            'organization_id', NEW.organization_id,
            'event', TG_ARGV[0]
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- New pending/planned task → dispatch immediately
DROP TRIGGER IF EXISTS task_created_notify ON tasks;
CREATE TRIGGER task_created_notify
    AFTER INSERT ON tasks
    FOR EACH ROW
    WHEN (NEW.status IN ('pending', 'planned'))
    EXECUTE FUNCTION notify_task_ready('created');

-- Task completed/failed/cancelled → may unblock next task in chain
DROP TRIGGER IF EXISTS task_completed_notify ON tasks;
CREATE TRIGGER task_completed_notify
    AFTER UPDATE OF status ON tasks
    FOR EACH ROW
    WHEN (OLD.status IN ('claimed', 'running')
      AND NEW.status IN ('completed', 'failed', 'cancelled'))
    EXECUTE FUNCTION notify_task_ready('completed');

-- Task released back to pending → re-dispatch
DROP TRIGGER IF EXISTS task_released_notify ON tasks;
CREATE TRIGGER task_released_notify
    AFTER UPDATE OF status ON tasks
    FOR EACH ROW
    WHEN (OLD.status IN ('claimed', 'running')
      AND NEW.status IN ('pending', 'planned'))
    EXECUTE FUNCTION notify_task_ready('released');
