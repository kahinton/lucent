-- Migration 023: Add performance indexes identified during code review.
--
-- These indexes address:
-- 1. Memory access control queries (OR on user_id / org_id + shared)
-- 2. API key expiration cleanup
-- 3. Sandbox template name lookups

-- Memory access control: the access query uses OR (user_id = $1 OR (org_id = $2 AND shared))
-- A composite index on (user_id, deleted_at) helps the first branch
CREATE INDEX IF NOT EXISTS idx_memories_user_active
    ON memories (user_id, deleted_at)
    WHERE deleted_at IS NULL;

-- API key expiration: enables efficient cleanup of expired keys
CREATE INDEX IF NOT EXISTS idx_api_keys_expires_at
    ON api_keys (expires_at)
    WHERE is_active = true AND expires_at IS NOT NULL;

-- Sandbox template name lookups within an org
CREATE INDEX IF NOT EXISTS idx_sandbox_templates_org_name
    ON sandbox_templates (organization_id, name);
