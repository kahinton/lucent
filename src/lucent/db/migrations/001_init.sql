-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Set similarity threshold for fuzzy matching
SET pg_trgm.similarity_threshold = 0.3;

-- Create enum-like constraint for memory types
-- Using CHECK constraint instead of ENUM for flexibility

-- Create the memories table
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('experience', 'technical', 'procedural', 'goal', 'individual')),
    content TEXT NOT NULL,
    tags TEXT[] DEFAULT '{}',
    importance INTEGER DEFAULT 5 CHECK (importance >= 1 AND importance <= 10),
    related_memory_ids UUID[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
);

-- Create indexes for efficient querying

-- Index for fuzzy text search on content using trigrams
CREATE INDEX IF NOT EXISTS idx_memories_content_trgm 
ON memories USING GIN (content gin_trgm_ops);

-- Index for tag searches
CREATE INDEX IF NOT EXISTS idx_memories_tags 
ON memories USING GIN (tags);

-- Index for JSONB metadata searches
CREATE INDEX IF NOT EXISTS idx_memories_metadata 
ON memories USING GIN (metadata);

-- Index for type filtering
CREATE INDEX IF NOT EXISTS idx_memories_type 
ON memories (type);

-- Index for username filtering
CREATE INDEX IF NOT EXISTS idx_memories_username 
ON memories (username);

-- Index for date range queries
CREATE INDEX IF NOT EXISTS idx_memories_created_at 
ON memories (created_at);

-- Index for soft delete filtering (partial index for active records)
CREATE INDEX IF NOT EXISTS idx_memories_active 
ON memories (id) WHERE deleted_at IS NULL;

-- Composite index for common query patterns
CREATE INDEX IF NOT EXISTS idx_memories_user_type_active 
ON memories (username, type, created_at DESC) WHERE deleted_at IS NULL;

-- Function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger to auto-update updated_at on row changes
DROP TRIGGER IF EXISTS update_memories_updated_at ON memories;
CREATE TRIGGER update_memories_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Add comment describing the table
COMMENT ON TABLE memories IS 'Stores LLM memories with support for multiple types, fuzzy search, and soft deletion';
COMMENT ON COLUMN memories.type IS 'Memory type: experience, technical, procedural, goal, or individual';
COMMENT ON COLUMN memories.importance IS 'Importance rating from 1 (routine) to 10 (essential)';
COMMENT ON COLUMN memories.related_memory_ids IS 'Array of UUIDs linking to related memories';
COMMENT ON COLUMN memories.metadata IS 'Type-specific metadata stored as JSONB';
COMMENT ON COLUMN memories.deleted_at IS 'Soft delete timestamp; NULL means active';
