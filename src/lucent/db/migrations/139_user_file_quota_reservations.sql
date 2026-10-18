-- Migration 139: In-flight quota reservations for user file writes
--
-- Quota used to be enforced inside the transaction that inserted the file row,
-- which runs *after* the bytes have already been written to the storage
-- provider. An over-quota upload therefore transmitted and stored its whole
-- payload only to be rejected and compensatorily deleted.
--
-- A reservation is taken under the same per-organization advisory lock before
-- the provider write. It counts toward the quota while it is live, so two
-- concurrent uploads cannot jointly reserve past the cap, and it expires on
-- its own if the process dies between reserve and finalise. The final insert
-- measures again with its own reservation excluded and is authoritative.
--
-- Deleted files keep counting until the provider's soft-delete retention
-- window passes (see files.soft_delete_retention_days), so retention and quota
-- accounting agree.

CREATE TABLE IF NOT EXISTS user_file_reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reserved_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_user_file_reservations_bytes_nonnegative CHECK (reserved_bytes >= 0)
);

CREATE INDEX IF NOT EXISTS idx_user_file_reservations_org_expiry
    ON user_file_reservations(organization_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_user_file_reservations_owner_expiry
    ON user_file_reservations(organization_id, user_id, expires_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON user_file_reservations TO lucent_daemon;

COMMENT ON TABLE user_file_reservations IS
  'Short-lived in-flight byte reservations that hold quota while a file write is between its pre-write check and its final insert. Expired rows no longer count.';
