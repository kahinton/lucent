-- Migration 144: Hard-cancel signals for in-flight tasks
--
-- Cancelling a claimed/running task used to be a silent no-op: the repository
-- refused the transition because the owning daemon session still held the
-- claim, and nothing told that session to stop. Users saw "cancel" do nothing
-- while the task kept burning model time and possibly mutating a repository.
--
-- Two columns make the intent explicit and durable:
--   cancel_requested_at — when a caller asked for this task to stop
--   cancel_requested_by — who asked
--
-- A trigger publishes the request on the 'task_cancel' channel so the daemon
-- holding the claim can interrupt exactly that session. The columns are the
-- source of truth; NOTIFY is only the low-latency path. If the notification is
-- dropped (LISTEN reconnect, replica restart), the daemon's heartbeat sweep
-- and the stale-task reaper both finalize the requested cancel from the
-- columns, so a cancel can never be lost.
--
-- Finalization is deliberately NOT done here. Only the claim owner (or the
-- reaper, once the claim is stale) may move a claimed/running task to
-- 'cancelled' — doing it in the trigger would race the live session's own
-- completion write.

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS cancel_requested_by UUID REFERENCES users(id) ON DELETE SET NULL;

COMMENT ON COLUMN tasks.cancel_requested_at IS
    'Set when a user asked for this claimed/running task to stop. The owning daemon session interrupts and then finalizes the task to cancelled.';

COMMENT ON COLUMN tasks.cancel_requested_by IS
    'User who requested the cancel, retained for audit.';

-- Partial index: the daemon heartbeat sweep and stale-task reaper ask
-- "which claimed/running tasks have an outstanding cancel request?".
CREATE INDEX IF NOT EXISTS idx_tasks_cancel_requested
    ON tasks (organization_id, cancel_requested_at)
    WHERE cancel_requested_at IS NOT NULL;

-- Notify the daemon that a cancel was requested. Fires only on the
-- NULL -> NOT NULL transition so repeated cancel clicks do not spam.
CREATE OR REPLACE FUNCTION notify_task_cancel()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'task_cancel',
        jsonb_build_object(
            'task_id', NEW.id,
            'request_id', NEW.request_id,
            'organization_id', NEW.organization_id,
            'requested_by', NEW.cancel_requested_by
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS task_cancel_notify ON tasks;
CREATE TRIGGER task_cancel_notify
    AFTER UPDATE OF cancel_requested_at ON tasks
    FOR EACH ROW
    WHEN (OLD.cancel_requested_at IS NULL AND NEW.cancel_requested_at IS NOT NULL)
    EXECUTE FUNCTION notify_task_cancel();
