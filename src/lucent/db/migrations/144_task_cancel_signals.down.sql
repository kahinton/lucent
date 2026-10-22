DROP TRIGGER IF EXISTS task_cancel_notify ON tasks;
DROP FUNCTION IF EXISTS notify_task_cancel();

DROP INDEX IF EXISTS idx_tasks_cancel_requested;

ALTER TABLE tasks
    DROP COLUMN IF EXISTS cancel_requested_by;

ALTER TABLE tasks
    DROP COLUMN IF EXISTS cancel_requested_at;
