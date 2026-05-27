-- Migration 036: Normalize request status 'planning' → 'planned'
-- Fixes mixed use of 'planning' vs 'planned' that could strand requests.

-- 1. Normalize any existing rows that use the non-canonical 'planning' value.
UPDATE requests SET status = 'planned', updated_at = NOW()
WHERE status = 'planning';

-- 2. Recreate the dedup partial unique index (from migration 032) without 'planning'.
DROP INDEX IF EXISTS idx_requests_org_fingerprint_open;
CREATE UNIQUE INDEX idx_requests_org_fingerprint_open
ON requests (organization_id, fingerprint)
WHERE status IN ('pending', 'planned', 'in_progress');

-- 3. Add CHECK constraint so only canonical status values are accepted.
ALTER TABLE requests DROP CONSTRAINT IF EXISTS chk_requests_status;
ALTER TABLE requests ADD CONSTRAINT chk_requests_status
    CHECK (status IN ('pending', 'planned', 'in_progress', 'completed', 'failed', 'cancelled'));
