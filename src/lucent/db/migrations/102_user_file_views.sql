-- Migration 102: Per-user latest-version tracking for user files
--
-- A file is unread when its current revision is newer than the revision the
-- owning user last viewed. The row is intentionally scoped to the file owner
-- and organization so it cannot become an authorization bypass.

CREATE TABLE IF NOT EXISTS user_file_views (
    file_id UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_viewed_revision INTEGER NOT NULL CHECK (last_viewed_revision > 0),
    last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (file_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_user_file_views_owner
    ON user_file_views(organization_id, user_id, file_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON user_file_views TO lucent_daemon;

COMMENT ON TABLE user_file_views IS
  'The latest revision each authorized file owner has viewed; used for Files attention badges.';