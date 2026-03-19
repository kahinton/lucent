-- Migration 035: Add CHECK constraint on tasks.sequence_order
-- Enforces non-negative execution order at the database level.

-- Sanitize any existing rows with negative sequence_order.
UPDATE tasks SET sequence_order = 0 WHERE sequence_order < 0;

ALTER TABLE tasks
    ADD CONSTRAINT chk_tasks_sequence_order
    CHECK (sequence_order >= 0);
