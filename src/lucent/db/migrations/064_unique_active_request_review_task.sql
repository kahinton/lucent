-- Prevent duplicate active "Post-completion review" tasks per request.
--
-- Background: the daemon's _ensure_request_review_tasks loop runs each
-- dispatch cycle. Without a DB-level constraint, two near-simultaneous
-- cycles (or two daemon instances) can both pass the in-memory dedup
-- check and INSERT a second review task before either commit becomes
-- visible. This caused us to occasionally see two duplicate review
-- tasks racing on the same request (observed 1.24s apart in production).
--
-- A partial unique index makes the second INSERT fail with a unique
-- violation, which the daemon code can catch and treat as "another
-- worker beat me to it" — the correct outcome.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_request_review_task
    ON tasks (request_id)
    WHERE title = 'Post-completion review'
      AND status NOT IN ('completed', 'failed', 'cancelled');
