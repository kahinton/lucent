-- Migration 169: Semantic memory embeddings (pgvector) — schema only.
--
-- Adds a 384-dimension embedding plus its provenance to `memories`. The vector
-- is computed in the application (embedding provider) and written by
-- application code; PostgreSQL only stores, indexes and ranks it. There is
-- deliberately NO NOT NULL constraint, NO default, and NO trigger that
-- computes embeddings: an embedding is model-dependent and expensive, and a
-- database-side default/trigger would silently fabricate vectors.
--
-- `embedding_model` records which provider/model produced the vector and is
-- the compatibility key for lookup: vectors from different models are not
-- comparable, so callers filter on it. `embedded_at` is the application write
-- time (NULL means never embedded), used by the backfill job to find rows.
--
-- Requires the `vector` extension (pgvector). The server image MUST be
-- pgvector/pgvector:pg16 BEFORE this migration runs; see docker-compose.yml
-- and lucent.db.prerequisites.REQUIRED_EXTENSIONS.
--
-- Idempotent: re-applying is a no-op (IF NOT EXISTS throughout).
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding vector(384);

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_model TEXT;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;

-- HNSW approximate-nearest-neighbour index for cosine distance. m=16 and
-- ef_construction=64 are pgvector's recommended defaults: for 384-dim
-- embeddings and a corpus in the 10^4-10^5 row range they give recall in the
-- high 0.9s at a moderate build cost and index size. (`hnsw.ef_search` is a
-- per-session query-time setting, not part of the index definition.) Cosine
-- distance is what the `<=>` operator computes and matches normalised text
-- embeddings.
CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Scoped-lookup support. The existing memory search path scopes by
-- organization_id (src/lucent/db/memory.py appends
-- `organization_id = $n::uuid`), not by user_id; ownership is enforced in the
-- application clearance layer. Semantic search therefore filters
--   WHERE organization_id = $1 AND embedding_model = $2
--     AND deleted_at IS NULL AND embedding IS NOT NULL
--   ORDER BY embedding <=> $3 LIMIT $4
-- This btree serves that predicate pair (the org + model filter, and the
-- pending-backfill scan); the HNSW index above serves the ORDER BY. Partial,
-- so it stays small while many rows are still un-embedded.
CREATE INDEX IF NOT EXISTS idx_memories_org_embedding_model
    ON memories (organization_id, embedding_model)
    WHERE deleted_at IS NULL AND embedding IS NOT NULL;

COMMENT ON COLUMN memories.embedding IS
    '384-dim text embedding (pgvector), written by the application; NULL until embedded.';
COMMENT ON COLUMN memories.embedding_model IS
    'Provider/model identifier that produced embedding; vectors from different models are not comparable.';
COMMENT ON COLUMN memories.embedded_at IS
    'When embedding was last written; NULL means never embedded.';
