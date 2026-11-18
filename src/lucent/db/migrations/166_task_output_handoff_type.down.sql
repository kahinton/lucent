-- Down migration for 166: remove 'handoff' from the task output types.
--
-- Any 'handoff' outputs written while the forward migration was applied would
-- violate the restored CHECK, so they are deleted first. These rows are a
-- derived mirror of the handoff interaction itself: the handoff, its thread,
-- and its references live in user_interactions and are untouched. Re-running
-- the forward migration after this rollback lets send_handoff recreate the
-- output row on the next handoff; already-sent handoffs keep their interaction
-- rows and can be re-recorded explicitly if needed.
--
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

DELETE FROM task_outputs WHERE output_type = 'handoff';

ALTER TABLE task_outputs
    DROP CONSTRAINT IF EXISTS ck_task_outputs_type;

ALTER TABLE task_outputs
    ADD CONSTRAINT ck_task_outputs_type CHECK (
        output_type IN (
            'link', 'github_issue', 'github_pr', 'email', 'document',
            'file', 'memory', 'deployment', 'artifact', 'other'
        )
    );
