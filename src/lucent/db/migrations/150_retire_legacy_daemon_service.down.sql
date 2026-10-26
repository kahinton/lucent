-- Rollback migration 150.
--
-- Re-activates the legacy `daemon-service` account. Clearances are NOT moved
-- back: the previous owner is not recorded per row, so the original assignment
-- cannot be reconstructed. Re-granting them by hand is the correct recovery —
-- and, given the account was retired because it held grants it could never use,
-- restoring them is unlikely to be what anyone wants.

UPDATE users
SET is_active = true,
    updated_at = NOW()
WHERE external_id = 'daemon-service';

COMMENT ON COLUMN users.external_id IS NULL;
