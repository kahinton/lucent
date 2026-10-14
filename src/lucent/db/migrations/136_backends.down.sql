-- Down migration for 136: remove first-class engine backends.
-- Order matters: drop dependents before the table they reference.

DROP INDEX IF EXISTS idx_tasks_backend;
ALTER TABLE tasks DROP COLUMN IF EXISTS backend_id;

DROP INDEX IF EXISTS idx_models_backend;
ALTER TABLE models DROP COLUMN IF EXISTS backend_id;

DROP INDEX IF EXISTS idx_backends_engine_provider;
DROP INDEX IF EXISTS idx_backends_org_engine;
DROP TABLE IF EXISTS backends;
