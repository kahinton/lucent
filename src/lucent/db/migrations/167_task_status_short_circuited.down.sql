-- Down migration for 167: remove the tasks.status CHECK constraint.
--
-- The pre-migration state was no status constraint at all (migration 015 only
-- documented the values in a comment; nothing constrained them). Dropping the
-- constraint restores that exact behaviour, so rows already written with
-- 'short_circuited' remain readable — no data rewrite is needed.
--
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

ALTER TABLE tasks
    DROP CONSTRAINT IF EXISTS chk_tasks_status;
