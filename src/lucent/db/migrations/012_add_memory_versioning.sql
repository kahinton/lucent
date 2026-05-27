-- Migration: Add memory versioning
-- Adds version tracking to memories and full snapshots to the audit log
-- for point-in-time restoration of any previous memory state.

-- Add version column to memories (all existing memories become version 1)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

-- Add version and snapshot columns to audit log
ALTER TABLE memory_audit_log ADD COLUMN IF NOT EXISTS version INTEGER;
ALTER TABLE memory_audit_log ADD COLUMN IF NOT EXISTS snapshot JSONB;

-- Add 'restore' to the allowed action types if not already present
-- (It's already in the CHECK constraint from 005, so no change needed)

-- Index for fast version lookups per memory
CREATE INDEX IF NOT EXISTS idx_audit_memory_version
ON memory_audit_log (memory_id, version DESC)
WHERE version IS NOT NULL;

-- Backfill: set version=1 on existing 'create' audit entries
UPDATE memory_audit_log SET version = 1 WHERE action_type = 'create' AND version IS NULL;

-- Backfill: assign incrementing versions to existing 'update' entries per memory
-- Uses a CTE to number updates chronologically per memory, starting at 2 (after create)
WITH numbered AS (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY memory_id ORDER BY created_at) + 1 as ver
    FROM memory_audit_log
    WHERE action_type = 'update' AND version IS NULL
)
UPDATE memory_audit_log
SET version = numbered.ver
FROM numbered
WHERE memory_audit_log.id = numbered.id;

-- Backfill: update the version column on memories to match their latest audit version
WITH latest_versions AS (
    SELECT memory_id, MAX(version) as max_ver
    FROM memory_audit_log
    WHERE version IS NOT NULL
    GROUP BY memory_id
)
UPDATE memories
SET version = latest_versions.max_ver
FROM latest_versions
WHERE memories.id::text = latest_versions.memory_id::text
  AND latest_versions.max_ver > 1;

COMMENT ON COLUMN memories.version IS 'Current version number, incremented on each update';
COMMENT ON COLUMN memory_audit_log.version IS 'Version number this entry represents';
COMMENT ON COLUMN memory_audit_log.snapshot IS 'Full memory state at this version (for point-in-time restore)';
