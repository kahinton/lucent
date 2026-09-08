-- Migration 110: Multi-session login.
--
-- Moves web session state from a single slot on `users` (session_token +
-- session_expires_at, migration 011) to a dedicated `user_sessions` table so a
-- user can hold N concurrent active sessions: logging in on one device never
-- invalidates another. Each session is independently validatable and
-- revocable (per-device logout).
--
-- Existing single-session state is carried over: a user's current token row
-- becomes a real session row, so nobody is logged out by this migration.
-- The legacy columns are dropped at the end.

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Carry existing sessions forward before the legacy columns go away.
INSERT INTO user_sessions (token_hash, user_id, expires_at, last_seen_at)
SELECT session_token, id, session_expires_at, NOW()
FROM users
WHERE session_token IS NOT NULL
  AND session_expires_at IS NOT NULL
ON CONFLICT (token_hash) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at ON user_sessions (expires_at);

COMMENT ON TABLE user_sessions IS
    'Active web login sessions. One row per device; tokens stored as SHA-256 hashes. Multiple rows per user are expected.';

ALTER TABLE users DROP COLUMN IF EXISTS session_token;
ALTER TABLE users DROP COLUMN IF EXISTS session_expires_at;