-- Migration 032: Request creation deduplication
-- Prevents duplicate requests from concurrent daemon cognitive cycles
-- via a fingerprint column + partial unique index on open lifecycle states.

ALTER TABLE requests ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(64);

-- Backfill fingerprints for existing rows
UPDATE requests SET fingerprint = md5(lower(trim(title)))
WHERE fingerprint IS NULL;

-- Partial unique index: only one open request per org with the same title fingerprint.
-- Completed/failed/cancelled requests don't block new ones with the same title.
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_org_fingerprint_open
ON requests (organization_id, fingerprint)
WHERE status IN ('pending', 'planning', 'planned', 'in_progress');
