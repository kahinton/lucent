DROP INDEX IF EXISTS idx_requests_backend;

ALTER TABLE requests
    DROP COLUMN IF EXISTS requested_model;

ALTER TABLE requests
    DROP COLUMN IF EXISTS backend_id;
