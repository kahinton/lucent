-- Add a prompt text column to schedules for free-form agent instructions.
-- This replaces the JSON task_template as the primary way to tell the agent what to do.

ALTER TABLE schedules ADD COLUMN IF NOT EXISTS prompt TEXT DEFAULT '';
