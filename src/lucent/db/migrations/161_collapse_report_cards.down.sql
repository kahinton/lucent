-- Down: migration 161 cannot be meaningfully reversed.
--
-- It hides superseded report cards, and the set of rows it hid is not recorded
-- anywhere. Un-hiding "everything not newest" would be a re-derivation that
-- could also reveal cards hidden for other reasons.
--
-- Re-running the migration is safe and idempotent; that is the intended
-- recovery path if a newer card is ever lost.
SELECT 1;
