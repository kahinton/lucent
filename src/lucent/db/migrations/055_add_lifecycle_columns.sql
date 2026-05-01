-- Migration 055: Add memory lifecycle columns
-- Adds lifecycle_stage, vitality_score, and vitality_computed_at to memories table.
-- Part of Memory Lifecycle Phase 1 (shadow mode) — design doc §8.1.

-- Add lifecycle_stage column
ALTER TABLE memories
ADD COLUMN IF NOT EXISTS lifecycle_stage TEXT NOT NULL DEFAULT 'active'
CHECK (lifecycle_stage IN ('active', 'consolidating', 'archived', 'forgotten'));

-- Add vitality_score for caching the computed score (nullable)
ALTER TABLE memories
ADD COLUMN IF NOT EXISTS vitality_score REAL;

-- Add vitality_computed_at to track when score was last calculated
ALTER TABLE memories
ADD COLUMN IF NOT EXISTS vitality_computed_at TIMESTAMP WITH TIME ZONE;

-- Index for lifecycle-aware queries
CREATE INDEX IF NOT EXISTS idx_memories_lifecycle_stage
ON memories (lifecycle_stage, vitality_score DESC)
WHERE deleted_at IS NULL;

-- Index for consolidation candidates
CREATE INDEX IF NOT EXISTS idx_memories_consolidation_candidates
ON memories (type, created_at ASC)
WHERE lifecycle_stage = 'consolidating' AND deleted_at IS NULL;

-- Index for forgetting candidates
CREATE INDEX IF NOT EXISTS idx_memories_forget_candidates
ON memories (lifecycle_stage, updated_at ASC)
WHERE lifecycle_stage = 'archived' AND deleted_at IS NULL;

COMMENT ON COLUMN memories.lifecycle_stage IS 'Lifecycle stage: active, consolidating, archived, or forgotten';
COMMENT ON COLUMN memories.vitality_score IS 'Cached vitality score (0.0-1.0). NULL means not yet computed.';
COMMENT ON COLUMN memories.vitality_computed_at IS 'When vitality_score was last computed by lifecycle scoring';
