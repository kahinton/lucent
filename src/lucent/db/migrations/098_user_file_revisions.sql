-- Migration 098: File creation provenance and immutable revision history

ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS origin_session_id UUID REFERENCES llm_sessions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS origin_turn_id UUID,
    ADD COLUMN IF NOT EXISTS origin_message_id UUID REFERENCES llm_messages(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS current_revision INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS user_file_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    edited_by UUID REFERENCES users(id) ON DELETE SET NULL,

    provider VARCHAR(64) NOT NULL,
    storage_key TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    checksum_sha256 CHAR(64) NOT NULL,
    change_summary TEXT,

    session_id UUID REFERENCES llm_sessions(id) ON DELETE SET NULL,
    turn_id UUID,
    message_id UUID REFERENCES llm_messages(id) ON DELETE SET NULL,
    request_id UUID REFERENCES requests(id) ON DELETE SET NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_user_file_revision_number UNIQUE (file_id, revision_number),
    CONSTRAINT uq_user_file_revision_storage UNIQUE (provider, storage_key),
    CONSTRAINT ck_user_file_revision_number CHECK (revision_number > 0),
    CONSTRAINT ck_user_file_revision_size CHECK (size_bytes >= 0),
    CONSTRAINT ck_user_file_revision_checksum CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
);

INSERT INTO user_file_revisions (
    file_id, organization_id, user_id, revision_number, edited_by,
    provider, storage_key, size_bytes, checksum_sha256,
    request_id, task_id, created_at
)
SELECT id, organization_id, user_id, 1, created_by,
       provider, storage_key, size_bytes, checksum_sha256,
       request_id, task_id, created_at
FROM user_files
ON CONFLICT (file_id, revision_number) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_user_file_revisions_file_recent
    ON user_file_revisions(file_id, revision_number DESC);
CREATE INDEX IF NOT EXISTS idx_user_file_revisions_owner_recent
    ON user_file_revisions(organization_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_files_origin_session
    ON user_files(origin_session_id)
    WHERE origin_session_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON user_file_revisions TO lucent_daemon;

COMMENT ON TABLE user_file_revisions IS
  'Immutable user-file content revisions with the chat, request, and task context that produced each change.';