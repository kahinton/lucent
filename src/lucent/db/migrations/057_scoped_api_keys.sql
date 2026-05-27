-- Migration 057: Scoped API keys for multi-user memory isolation
-- Adds memory_scope_user_id and memory_scope columns to api_keys.
-- When set, these restrict memory operations performed with the key
-- to a specific user's memories or to org-shared memories only.

ALTER TABLE api_keys
ADD COLUMN memory_scope_user_id UUID NULL REFERENCES users(id),
ADD COLUMN memory_scope TEXT NULL
    CHECK (memory_scope IN ('user', 'org_shared_only'));

-- If scope is 'user', a target user must be specified.
ALTER TABLE api_keys
ADD CONSTRAINT chk_scope_user_requires_user_id
    CHECK (memory_scope != 'user' OR memory_scope_user_id IS NOT NULL);

-- If scope is 'org_shared_only', no target user should be set.
ALTER TABLE api_keys
ADD CONSTRAINT chk_scope_org_shared_no_user_id
    CHECK (memory_scope != 'org_shared_only' OR memory_scope_user_id IS NULL);

-- Index for cleanup / lookup queries filtering by scoped user.
CREATE INDEX idx_api_keys_memory_scope_user_id
ON api_keys (memory_scope_user_id)
WHERE memory_scope_user_id IS NOT NULL;
