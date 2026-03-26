-- Migration 044: Add request review lifecycle state + metadata
-- Extends request lifecycle to support:
-- pending -> in_progress -> review -> completed / needs_rework
-- and needs_rework -> in_progress.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS review_count INT NOT NULL DEFAULT 0;

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS max_reviews INT NOT NULL DEFAULT 3;

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS review_feedback TEXT;

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;

ALTER TABLE requests DROP CONSTRAINT IF EXISTS chk_requests_status;
ALTER TABLE requests ADD CONSTRAINT chk_requests_status
    CHECK (
        status IN (
            'pending',
            'planned',
            'in_progress',
            'review',
            'needs_rework',
            'completed',
            'failed',
            'cancelled'
        )
    );

-- Keep open-request dedup behavior for new active statuses.
DROP INDEX IF EXISTS idx_requests_org_fingerprint_open;
CREATE UNIQUE INDEX idx_requests_org_fingerprint_open
ON requests (organization_id, fingerprint)
WHERE status IN ('pending', 'planned', 'in_progress', 'review', 'needs_rework');

-- Helpful for review queue scans.
CREATE INDEX IF NOT EXISTS idx_requests_org_review_status
    ON requests(organization_id, status, updated_at DESC)
    WHERE status IN ('review', 'needs_rework');
