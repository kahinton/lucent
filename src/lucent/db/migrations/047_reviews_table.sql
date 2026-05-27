-- Migration 047: First-class reviews table
--
-- Moves reviews from memory-tag-based storage into a dedicated table.
-- Reviews represent approval/rejection decisions on requests and tasks.
--
-- Addresses design review feedback:
--   - organization_id column for direct multi-tenant scoping (GPT Codex)
--   - reviewer_user_id FK for accountability and authorization (GPT Codex)
--   - pg_notify trigger for daemon wake signals (requirement)
--   - Data migration from request-level review_feedback (requirement)
--   - Reversible: DOWN section included

-- ── UP ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reviews (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id      UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    task_id         UUID REFERENCES tasks(id) ON DELETE SET NULL,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    reviewer_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    reviewer_display_name VARCHAR(256),
    status          VARCHAR(16) NOT NULL,
    comments        TEXT,
    source          VARCHAR(32) NOT NULL DEFAULT 'human',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_review_status CHECK (status IN ('approved', 'rejected')),
    CONSTRAINT chk_review_source CHECK (source IN ('human', 'daemon', 'agent'))
);

-- Primary query patterns:
--   List reviews by org (activity page)
CREATE INDEX IF NOT EXISTS idx_reviews_org_created
    ON reviews(organization_id, created_at DESC);

--   Reviews for a specific request
CREATE INDEX IF NOT EXISTS idx_reviews_request
    ON reviews(request_id, created_at DESC);

--   Reviews for a specific task
CREATE INDEX IF NOT EXISTS idx_reviews_task
    ON reviews(task_id, created_at DESC)
    WHERE task_id IS NOT NULL;

--   Filter by status within an org
CREATE INDEX IF NOT EXISTS idx_reviews_org_status
    ON reviews(organization_id, status, created_at DESC);

-- Trigger: notify daemon on review creation (reuses existing 'request_ready' channel)
CREATE OR REPLACE FUNCTION notify_review_created()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('request_ready',
        json_build_object(
            'type', 'review_created',
            'review_id', NEW.id,
            'request_id', NEW.request_id,
            'status', NEW.status
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_review_created
    AFTER INSERT ON reviews
    FOR EACH ROW
    EXECUTE FUNCTION notify_review_created();

-- Data migration: copy existing request-level review decisions into reviews table.
-- Only migrates rows where review_feedback was actually recorded.
-- Uses a CTE to avoid inserting duplicates if migration is re-run.
INSERT INTO reviews (request_id, organization_id, reviewer_user_id,
                     status, comments, source, created_at)
SELECT
    r.id,
    r.organization_id,
    r.created_by,
    CASE
        WHEN r.status = 'completed' AND r.reviewed_at IS NOT NULL THEN 'approved'
        ELSE 'rejected'
    END,
    r.review_feedback,
    'daemon',
    COALESCE(r.reviewed_at, r.updated_at)
FROM requests r
WHERE r.review_feedback IS NOT NULL
  AND r.reviewed_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM reviews rv
      WHERE rv.request_id = r.id
        AND rv.created_at = COALESCE(r.reviewed_at, r.updated_at)
  );


-- ── DOWN (rollback) ─────────────────────────────────────────────────────
-- To reverse this migration:
--
--   DROP TRIGGER IF EXISTS trg_review_created ON reviews;
--   DROP FUNCTION IF EXISTS notify_review_created();
--   DROP TABLE IF EXISTS reviews;
