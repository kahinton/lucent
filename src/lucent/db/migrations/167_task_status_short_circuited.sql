-- Migration 167: Add the terminal task status for a gated flow stop.
--
-- A flow gate that evaluates false stops the remaining work without being an
-- error: the gating task completes successfully, and the pending/planned tasks
-- behind it become 'short_circuited'. It must never be 'cancelled' — under a
-- strict dependency policy a cancelled level has no completed row, so it
-- blocks every later stage forever.
--
-- There was no CHECK on tasks.status before this migration: migration 015 only
-- documented the allowed values in a column comment, and no later migration
-- constrained them. This adds one for the first time, with a strict superset
-- of every value the code writes today plus the new terminal value. It is
-- purely additive: no row is rewritten or backfilled, and PostgreSQL validates
-- existing rows when the constraint is added.
--
-- The constraint is dropped and re-added so the allowed set stays in one
-- readable place and the statement is re-runnable.
--
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

ALTER TABLE tasks
    DROP CONSTRAINT IF EXISTS chk_tasks_status;

ALTER TABLE tasks
    ADD CONSTRAINT chk_tasks_status CHECK (
        status IN (
            'pending',
            'planned',
            'claimed',
            'running',
            'completed',
            'failed',
            'cancelled',
            'needs_review',
            'short_circuited'
        )
    );
