-- Migration: Add memory access tracking
-- Tracks when memories are accessed (viewed or returned in search results)

-- Add last_accessed_at to memories for quick lookups
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMP WITH TIME ZONE;

-- Create index for finding recently/never accessed memories
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed 
ON memories (last_accessed_at DESC NULLS LAST) WHERE deleted_at IS NULL;

-- Create access log table for full history
CREATE TABLE IF NOT EXISTS memory_access_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- What memory was accessed
    memory_id UUID NOT NULL,
    
    -- Who accessed it
    user_id UUID,
    organization_id UUID,
    
    -- How it was accessed
    access_type TEXT NOT NULL CHECK (access_type IN (
        'view',           -- Direct get_memory call
        'search_result'   -- Returned in search results
    )),
    
    -- When it was accessed
    accessed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    
    -- Context about the access
    -- For search_result: includes the search query, filters used
    -- For view: could include referrer info
    context JSONB DEFAULT '{}'::jsonb
);

-- Indexes for common query patterns

-- Find all accesses for a specific memory (access history)
CREATE INDEX IF NOT EXISTS idx_access_memory_id 
ON memory_access_log (memory_id, accessed_at DESC);

-- Find all accesses by a specific user
CREATE INDEX IF NOT EXISTS idx_access_user_id 
ON memory_access_log (user_id, accessed_at DESC) WHERE user_id IS NOT NULL;

-- Find accesses within an organization
CREATE INDEX IF NOT EXISTS idx_access_org_id 
ON memory_access_log (organization_id, accessed_at DESC) WHERE organization_id IS NOT NULL;

-- Time-based queries (recent activity)
CREATE INDEX IF NOT EXISTS idx_access_time 
ON memory_access_log (accessed_at DESC);

-- Find accesses by type
CREATE INDEX IF NOT EXISTS idx_access_type 
ON memory_access_log (access_type, accessed_at DESC);

-- Comments
COMMENT ON COLUMN memories.last_accessed_at IS 'Timestamp of most recent access (view or search result)';
COMMENT ON TABLE memory_access_log IS 'Full history of memory accesses for analytics and auditing';
COMMENT ON COLUMN memory_access_log.access_type IS 'How the memory was accessed: view (direct) or search_result';
COMMENT ON COLUMN memory_access_log.context IS 'Access context: search query, filters, referrer, etc.';
