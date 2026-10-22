DROP INDEX IF EXISTS idx_requests_planner_undecomposed;

ALTER TABLE requests
    DROP CONSTRAINT IF EXISTS chk_requests_decomposition_mode;

ALTER TABLE requests
    DROP COLUMN IF EXISTS decomposition_mode;
