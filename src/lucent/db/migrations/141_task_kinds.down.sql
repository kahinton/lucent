DROP INDEX IF EXISTS idx_tasks_kind_status;

ALTER TABLE tasks
    DROP COLUMN IF EXISTS execution_config;

ALTER TABLE tasks
    DROP COLUMN IF EXISTS task_kind;
