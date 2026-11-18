-- Migration 166: Allow 'handoff' as a task/request output type.
--
-- A handoff sent by a task is the deliverable of that task, so send_handoff
-- records it as a request output pointing at /handoffs/{id}. The original
-- output-type CHECK (migration 078) predates handoffs as an output, so it must
-- accept the new value before the row can be written. Widening a CHECK is
-- purely additive: existing rows remain valid and no data is rewritten.
--
-- The constraint is dropped and re-added so the allowed set stays in one
-- readable place; PostgreSQL validates the existing rows when the new
-- constraint is added, and every existing value is still in the list.
--
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

ALTER TABLE task_outputs
    DROP CONSTRAINT IF EXISTS ck_task_outputs_type;

ALTER TABLE task_outputs
    ADD CONSTRAINT ck_task_outputs_type CHECK (
        output_type IN (
            'link', 'github_issue', 'github_pr', 'email', 'document',
            'file', 'memory', 'deployment', 'artifact', 'handoff', 'other'
        )
    );
