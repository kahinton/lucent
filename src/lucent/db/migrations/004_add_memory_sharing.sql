-- Migration: Add memory sharing within organizations
-- Enables users to share specific memories with others in their organization

-- Add shared flag to memories (defaults to false - private)
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS shared BOOLEAN DEFAULT false;

-- Add organization_id to memories for efficient org-scoped queries
-- This denormalizes the data but makes queries much faster
ALTER TABLE memories 
ADD COLUMN IF NOT EXISTS organization_id UUID;

-- Create index for shared memories within an organization
CREATE INDEX IF NOT EXISTS idx_memories_org_shared 
ON memories (organization_id, shared) WHERE deleted_at IS NULL AND shared = true;

-- Create index for user's own memories plus shared org memories
CREATE INDEX IF NOT EXISTS idx_memories_org_id 
ON memories (organization_id) WHERE deleted_at IS NULL;

-- Add foreign key constraint to organizations
ALTER TABLE memories
DROP CONSTRAINT IF EXISTS fk_memories_organization_id;

ALTER TABLE memories
ADD CONSTRAINT fk_memories_organization_id 
FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE;

-- Comments
COMMENT ON COLUMN memories.shared IS 'Whether this memory is shared with other users in the organization';
COMMENT ON COLUMN memories.organization_id IS 'Organization this memory belongs to (denormalized for query efficiency)';
