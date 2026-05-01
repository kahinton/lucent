-- Migration 060: GitHub repo access cache for memory ACL enforcement

CREATE TABLE IF NOT EXISTS github_repo_access_cache (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    repo_full_name TEXT NOT NULL,
    has_access BOOLEAN NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, repo_full_name)
);

CREATE INDEX IF NOT EXISTS idx_github_repo_access_cache_expires_at
ON github_repo_access_cache (expires_at);
