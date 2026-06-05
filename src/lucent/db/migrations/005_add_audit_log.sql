-- Migration: Add memory audit log for tracking changes
-- Tracks all modifications to memories for administrative oversight

-- Enum-like type for audit actions
-- Using TEXT with CHECK constraint for flexibility

CREATE TABLE IF NOT EXISTS memory_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- What memory was affected
    memory_id UUID NOT NULL,
    
    -- Who made the change (NULL if system action or deleted user)
    user_id UUID,
    organization_id UUID,
    
    -- What type of action was performed
    action_type TEXT NOT NULL CHECK (action_type IN (
        'create',           -- Memory was created
        'update',           -- Memory content/metadata was updated
        'delete',           -- Memory was soft-deleted
        'restore',          -- Memory was restored from deletion
        'share',            -- Memory was shared with organization
        'unshare',          -- Memory sharing was revoked
        'hard_delete'       -- Memory was permanently deleted
    )),
    
    -- When the action occurred
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    
    -- What fields were changed (for updates)
    -- Stores array of field names that were modified
    changed_fields TEXT[],
    
    -- Previous values (JSONB for flexibility)
    -- Only populated for updates, stores old values of changed fields
    old_values JSONB,
    
    -- New values (JSONB for flexibility)
    -- For creates: full memory content
    -- For updates: new values of changed fields
    -- For deletes: NULL
    new_values JSONB,
    
    -- Additional context about the action
    -- Could include: IP address, user agent, API version, etc.
    context JSONB DEFAULT '{}'::jsonb,
    
    -- Free-form notes (e.g., reason for deletion)
    notes TEXT
);

-- Indexes for common query patterns

-- Find all audit entries for a specific memory
CREATE INDEX IF NOT EXISTS idx_audit_memory_id 
ON memory_audit_log (memory_id);

-- Find all actions by a specific user
CREATE INDEX IF NOT EXISTS idx_audit_user_id 
ON memory_audit_log (user_id) WHERE user_id IS NOT NULL;

-- Find all actions within an organization
CREATE INDEX IF NOT EXISTS idx_audit_org_id 
ON memory_audit_log (organization_id) WHERE organization_id IS NOT NULL;

-- Find actions by type (useful for finding all deletes, shares, etc.)
CREATE INDEX IF NOT EXISTS idx_audit_action_type 
ON memory_audit_log (action_type);

-- Time-based queries (find recent changes, changes in date range)
CREATE INDEX IF NOT EXISTS idx_audit_created_at 
ON memory_audit_log (created_at DESC);

-- Composite index for org + time queries (admin dashboard)
CREATE INDEX IF NOT EXISTS idx_audit_org_time 
ON memory_audit_log (organization_id, created_at DESC) 
WHERE organization_id IS NOT NULL;

-- Composite index for user + time queries (user activity)
CREATE INDEX IF NOT EXISTS idx_audit_user_time 
ON memory_audit_log (user_id, created_at DESC) 
WHERE user_id IS NOT NULL;

-- Comments for documentation
COMMENT ON TABLE memory_audit_log IS 'Audit trail for all memory modifications';
COMMENT ON COLUMN memory_audit_log.memory_id IS 'The memory that was affected (may no longer exist if hard deleted)';
COMMENT ON COLUMN memory_audit_log.user_id IS 'The user who performed the action (NULL for system actions)';
COMMENT ON COLUMN memory_audit_log.organization_id IS 'The organization context for the action';
COMMENT ON COLUMN memory_audit_log.action_type IS 'Type of action: create, update, delete, restore, share, unshare, hard_delete';
COMMENT ON COLUMN memory_audit_log.changed_fields IS 'List of field names that were modified (for updates)';
COMMENT ON COLUMN memory_audit_log.old_values IS 'Previous values of changed fields (for updates)';
COMMENT ON COLUMN memory_audit_log.new_values IS 'New values after the change';
COMMENT ON COLUMN memory_audit_log.context IS 'Additional context: IP, user agent, API version, etc.';
COMMENT ON COLUMN memory_audit_log.notes IS 'Optional notes about the action (e.g., deletion reason)';
