-- Migration 106: Wake the dispatcher when a reviewed or failed task is retried.
-- Retry transitions use needs_review/failed -> pending, which the original
-- task_released_notify trigger did not cover.

DROP TRIGGER IF EXISTS task_released_notify ON tasks;

CREATE TRIGGER task_released_notify
    AFTER UPDATE OF status ON tasks
    FOR EACH ROW
    WHEN (
        OLD.status IN ('claimed', 'running', 'failed', 'needs_review')
        AND NEW.status IN ('pending', 'planned')
    )
    EXECUTE FUNCTION notify_task_ready('released');