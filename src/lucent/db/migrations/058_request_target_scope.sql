-- Migration 058: Add target scope fields to requests
-- Tracks which repo/directories a request is acting on, enabling
-- automatic injection of relevant technical memories into task context.

ALTER TABLE requests
ADD COLUMN IF NOT EXISTS target_repo TEXT;

ALTER TABLE requests
ADD COLUMN IF NOT EXISTS target_paths TEXT[];

COMMENT ON COLUMN requests.target_repo IS 'Repository this request targets (owner/repo format). Used to inject relevant technical memories into task context.';
COMMENT ON COLUMN requests.target_paths IS 'Specific directories or files this request targets. Used to narrow technical memory injection.';

-- Index for finding requests by repo
CREATE INDEX IF NOT EXISTS idx_requests_target_repo
ON requests (target_repo)
WHERE target_repo IS NOT NULL;
