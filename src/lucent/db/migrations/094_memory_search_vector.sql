-- Migration 094: Add a weighted full-text search vector for memory retrieval.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS search_vector TSVECTOR;

CREATE OR REPLACE FUNCTION update_memory_search_vector()
RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(array_to_string(NEW.tags, ' '), '')), 'B') ||
        setweight(to_tsvector('simple', COALESCE(NEW.metadata::text, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_memory_search_vector_trigger ON memories;
CREATE TRIGGER update_memory_search_vector_trigger
    BEFORE INSERT OR UPDATE OF content, tags, metadata ON memories
    FOR EACH ROW
    EXECUTE FUNCTION update_memory_search_vector();

CREATE INDEX IF NOT EXISTS idx_memories_search_vector
    ON memories USING GIN (search_vector);

COMMENT ON COLUMN memories.search_vector IS
    'Weighted full-text document: content (A), tags (B), metadata (C).';