-- Migration 095: Backfill search vectors separately from the schema migration.

ALTER TABLE memories DISABLE TRIGGER update_memories_updated_at;

UPDATE memories
SET search_vector =
    setweight(to_tsvector('english', COALESCE(content, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(array_to_string(tags, ' '), '')), 'B') ||
    setweight(to_tsvector('simple', COALESCE(metadata::text, '')), 'C')
WHERE search_vector IS NULL;

ALTER TABLE memories ENABLE TRIGGER update_memories_updated_at;