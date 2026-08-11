-- Migration 097: User-owned files and durable artifact references
-- File bytes live behind a storage provider; this table owns identity,
-- authorization, provenance, and provider-independent metadata.

CREATE TABLE IF NOT EXISTS user_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    request_id UUID REFERENCES requests(id) ON DELETE SET NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,

    provider VARCHAR(64) NOT NULL DEFAULT 'local',
    storage_key TEXT NOT NULL,
    filename VARCHAR(512) NOT NULL,
    display_name VARCHAR(256) NOT NULL,
    mime_type VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream',
    size_bytes BIGINT NOT NULL,
    checksum_sha256 CHAR(64) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,

    CONSTRAINT ck_user_files_provider_nonempty CHECK (length(trim(provider)) > 0),
    CONSTRAINT ck_user_files_storage_key_nonempty CHECK (length(trim(storage_key)) > 0),
    CONSTRAINT ck_user_files_filename_nonempty CHECK (length(trim(filename)) > 0),
    CONSTRAINT ck_user_files_display_name_nonempty CHECK (length(trim(display_name)) > 0),
    CONSTRAINT ck_user_files_size_nonnegative CHECK (size_bytes >= 0),
    CONSTRAINT ck_user_files_checksum_sha256 CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_user_files_provider_key UNIQUE (provider, storage_key)
);

CREATE INDEX IF NOT EXISTS idx_user_files_owner_recent
    ON user_files(organization_id, user_id, created_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_files_request_recent
    ON user_files(request_id, created_at DESC)
    WHERE request_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_files_task_recent
    ON user_files(task_id, created_at DESC)
    WHERE task_id IS NOT NULL AND deleted_at IS NULL;

ALTER TABLE user_interaction_references
    DROP CONSTRAINT IF EXISTS ck_user_interaction_references_type;
ALTER TABLE user_interaction_references
    ADD CONSTRAINT ck_user_interaction_references_type CHECK (
        reference_type IN (
            'request', 'task', 'task_output', 'user_file', 'memory', 'workflow',
            'schedule_run', 'llm_session', 'url', 'other'
        )
    );

GRANT SELECT, INSERT, UPDATE, DELETE ON user_files TO lucent_daemon;

COMMENT ON TABLE user_files IS
  'User-owned file metadata. Content is stored by the named provider and access is scoped to organization_id plus user_id.';
COMMENT ON COLUMN user_files.storage_key IS
  'Opaque provider-native key. Never expose it directly as a download path or use it as an authorization boundary.';