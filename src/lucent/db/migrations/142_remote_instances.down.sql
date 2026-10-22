-- Migration 142 rollback
DROP INDEX IF EXISTS idx_tasks_remote_target;
ALTER TABLE tasks DROP COLUMN IF EXISTS remote_target_id;
DROP TABLE IF EXISTS remote_instances;