-- Migration: Add unique constraint for API key names per user
-- Prevents users from having multiple active keys with the same name

-- First, revoke duplicate keys (keep the oldest one, revoke newer duplicates)
-- This handles existing duplicates before adding the constraint
WITH duplicates AS (
    SELECT id, user_id, name,
           ROW_NUMBER() OVER (PARTITION BY user_id, name ORDER BY created_at ASC) as rn
    FROM api_keys
    WHERE revoked_at IS NULL
)
UPDATE api_keys
SET revoked_at = NOW(), is_active = false
WHERE id IN (
    SELECT id FROM duplicates WHERE rn > 1
);

-- Create a unique partial index for active (non-revoked) keys per user
-- This allows the same name to be reused after a key is revoked
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_user_name_active 
ON api_keys (user_id, name) 
WHERE revoked_at IS NULL;

-- Comment
COMMENT ON INDEX idx_api_keys_user_name_active IS 'Ensures each user can only have one active API key with a given name';
