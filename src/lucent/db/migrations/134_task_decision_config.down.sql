-- Down migration for 134: remove decision-step task payloads.

ALTER TABLE tasks DROP COLUMN IF EXISTS decision_config;