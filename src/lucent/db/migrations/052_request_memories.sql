-- Migration 052: Request-level memory links
--
-- A many-to-many table linking requests to memories.
-- Enables:
--   1. Goal-based dedup: don't re-create requests for memories that already
--      have active/recently-completed work
--   2. Programmatic goal completion: when a request completes, linked goal
--      memories can be auto-updated
--   3. Flexible linking: requests can reference goals, context, or any memory

CREATE TABLE IF NOT EXISTS request_memories (
    request_id UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    memory_id  UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation   VARCHAR(16) NOT NULL DEFAULT 'goal',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (request_id, memory_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_request_memories_memory
    ON request_memories (memory_id);
