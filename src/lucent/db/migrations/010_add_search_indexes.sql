-- Migration: Add performance indexes for search operations
-- Addresses potential performance issues with fuzzy search across fields

-- Note: We cannot add a trigram index on array_to_string(tags) because
-- array_to_string is not marked IMMUTABLE in PostgreSQL. The existing
-- GIN index on tags (for array containment) will be used instead.
-- For fuzzy tag search, the query will scan matching rows after other
-- filters (access control, deleted_at) have narrowed the result set.

-- Add composite index for access control pattern (very common query pattern)
-- Optimizes: (user_id = X OR (organization_id = Y AND shared = true))
CREATE INDEX IF NOT EXISTS idx_memories_user_id_active
ON memories (user_id) 
WHERE deleted_at IS NULL;

-- Add composite index for org + shared queries
CREATE INDEX IF NOT EXISTS idx_memories_org_shared_active
ON memories (organization_id) 
WHERE deleted_at IS NULL AND shared = true;

-- Add index on last_accessed_at for access analytics queries
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed
ON memories (last_accessed_at DESC)
WHERE deleted_at IS NULL AND last_accessed_at IS NOT NULL;

-- Comments
COMMENT ON INDEX idx_memories_user_id_active IS 'Optimizes access control queries by user ownership';
COMMENT ON INDEX idx_memories_org_shared_active IS 'Optimizes access control queries for shared memories';
COMMENT ON INDEX idx_memories_last_accessed IS 'Supports access analytics and recently-used queries';
