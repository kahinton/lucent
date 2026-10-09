-- Down migration for 135: remove fusion payloads.

ALTER TABLE tasks DROP COLUMN IF EXISTS fusion_config;