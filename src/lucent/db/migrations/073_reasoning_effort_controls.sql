-- Migration 073: Reasoning effort controls
-- Adds model-level allowed reasoning/thinking levels plus per-task/schedule selections.

ALTER TABLE models
    ADD COLUMN IF NOT EXISTS reasoning_efforts TEXT[] NOT NULL DEFAULT '{}';

ALTER TABLE tasks
                ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(64);

ALTER TABLE schedules
                ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(64);

-- Values are intentionally not seeded from Lucent heuristics. Provider sync
-- populates reasoning_efforts only when a provider model catalog reports exact
-- selectable levels for that model.

CREATE INDEX IF NOT EXISTS idx_models_reasoning_efforts
    ON models USING GIN (reasoning_efforts);
