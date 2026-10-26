-- Rollback migration 149.
--
-- Only the constraint is removed; organization names renamed by the forward
-- migration are left as they are. Restoring the pre-migration names is not
-- possible without a record of them, and the suffixed names remain valid.

DROP INDEX IF EXISTS idx_organizations_name_unique;
