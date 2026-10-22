-- Migration 143: Explicit request decomposition ownership
--
-- The background composer decided whether to decompose a request by testing
-- "has zero tasks". That test cannot distinguish:
--   * a request nobody has planned yet, from
--   * a request a chat agent deliberately planned (possibly partially).
-- When a chat agent authored a partial plan, the composer saw zero *new*
-- context and authored a second, divergent breakdown.
--
-- `decomposition_mode` makes ownership explicit:
--   'planner' — the daemon owns decomposition; the composer may decompose
--               while the request has no tasks.
--   'author'  — a caller (chat) owned the decomposition; the composer must
--               never add tasks. Authorship is sticky: adding a task later
--               must not hand the request back to the composer.
--
-- Existing requests stay 'planner' so historical zero-task backlog still
-- recovers. Requests that already carry a chat-authored plan are backfilled
-- to 'author' below using the trusted origin-session lineage recorded by
-- create_request (migration 075), so a partial historical plan cannot be
-- doubled by the composer after this migration.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS decomposition_mode VARCHAR(16) NOT NULL DEFAULT 'planner';

ALTER TABLE requests
    ADD CONSTRAINT chk_requests_decomposition_mode
    CHECK (decomposition_mode IN ('planner', 'author'));

-- Backfill: a request created from a chat session that already has tasks was
-- authored by that session, not by the planner (the planner never sets
-- origin_session_id). This keeps existing partially-planned requests safe.
UPDATE requests r
SET decomposition_mode = 'author'
WHERE r.origin_session_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM tasks t WHERE t.request_id = r.id)
  AND r.decomposition_mode = 'planner';

-- The composer's hot query is "planner-owned requests with no tasks", so keep
-- a narrow partial index for it rather than scanning all requests.
CREATE INDEX IF NOT EXISTS idx_requests_planner_undecomposed
    ON requests (organization_id, created_at)
    WHERE decomposition_mode = 'planner';
