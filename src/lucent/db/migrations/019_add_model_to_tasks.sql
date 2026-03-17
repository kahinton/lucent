-- Migration 019: Add model column to tasks and schedules
-- Allows per-task and per-schedule model selection for LLM dispatch

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS model VARCHAR(64);
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS model VARCHAR(64);
