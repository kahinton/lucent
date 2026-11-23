-- Down migration for 169: remove the memory embedding schema.
--
-- Drops the two indexes and the three columns, then the extension.
--
-- WARNING: DROP EXTENSION vector is destructive to anything else that depends
-- on pgvector. No CASCADE is used on purpose: if a restored dump or another
-- table still carries vector columns, this rollback aborts instead of
-- silently dropping their data. Note also that dropping the extension would
-- make a dump containing vector columns unrestorable until the extension is
-- re-created. The real rollback of record for this change is restoring the
-- pre-change snapshot; this file is for a clean reversal when no vector data
-- exists anywhere else.
--
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

DROP INDEX IF EXISTS idx_memories_embedding_hnsw;
DROP INDEX IF EXISTS idx_memories_org_embedding_model;

ALTER TABLE memories DROP COLUMN IF EXISTS embedded_at;
ALTER TABLE memories DROP COLUMN IF EXISTS embedding_model;
ALTER TABLE memories DROP COLUMN IF EXISTS embedding;

DROP EXTENSION IF EXISTS vector;
