-- Migration 056: Add consolidation metadata index
-- Supports efficient lookups for consolidated source linkage.
-- Part of Memory Lifecycle Phase 1 (shadow mode) — design doc §8.1.

CREATE INDEX IF NOT EXISTS idx_memories_consolidated_from
ON memories ((metadata->'consolidated_from'))
WHERE metadata ? 'consolidated_from' AND deleted_at IS NULL;
