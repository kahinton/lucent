-- Migration 141: Explicit task kinds and generic execution config
-- Task execution was inferred from nullable payload columns plus the magic
-- agent_type='decision-step'. This makes the kind explicit so future executors
-- (run-code, webhook, shell) can register without another schema change.
--
-- Legacy columns stay in place for compatibility: decision_config and
-- fusion_config are still written/read by older code. New code reads
-- task_kind/execution_config first and falls back to the legacy columns.

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS task_kind VARCHAR(32);

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS execution_config JSONB;

-- Backfill the three kinds that exist today. Decision/fusion payloads become
-- the generic execution_config; the legacy columns remain as mirrors.
UPDATE tasks
SET task_kind = 'decision'
WHERE decision_config IS NOT NULL AND task_kind IS NULL;

UPDATE tasks
SET task_kind = 'fusion'
WHERE fusion_config IS NOT NULL AND task_kind IS NULL;

UPDATE tasks
SET task_kind = 'agent'
WHERE task_kind IS NULL;

UPDATE tasks
SET execution_config = COALESCE(decision_config, fusion_config)
WHERE execution_config IS NULL
  AND (decision_config IS NOT NULL OR fusion_config IS NOT NULL);

ALTER TABLE tasks
    ALTER COLUMN task_kind SET DEFAULT 'agent';

ALTER TABLE tasks
    ALTER COLUMN task_kind SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_kind_status
    ON tasks(task_kind, status);
