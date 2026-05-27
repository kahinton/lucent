-- Migration 065: Add memory shadow forgetting sidecar table
-- Reference memo: 640b13a4-c9f6-4175-8770-715a9641f8c5
-- Creates shadow-only score storage without mutating the memories schema.

CREATE TABLE IF NOT EXISTS memory_shadow_scores (
    memory_id      UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    strategy       TEXT NOT NULL,
    score          REAL,
    shadow_action  TEXT,
    signals        JSONB NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    divergence_tag TEXT,
    PRIMARY KEY (memory_id, strategy, computed_at)
);

CREATE INDEX IF NOT EXISTS ix_msv_strategy_computed
    ON memory_shadow_scores (strategy, computed_at DESC);

CREATE INDEX IF NOT EXISTS ix_msv_divergence
    ON memory_shadow_scores (strategy, divergence_tag)
    WHERE divergence_tag IS NOT NULL;

-- Preflight prerequisite indexes for graph-signal reads.
CREATE INDEX IF NOT EXISTS idx_memories_related_memory_ids_gin
    ON memories USING GIN (related_memory_ids);

CREATE INDEX IF NOT EXISTS idx_access_memory_user
    ON memory_access_log (memory_id, user_id)
    WHERE user_id IS NOT NULL;
