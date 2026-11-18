-- Down migration for 165: make backends.organization_id nullable again and
-- restore the NULLS NOT DISTINCT uniqueness that allowed shared backends.
--
-- This is a SCHEMA rollback only, deliberately not a data restore.
-- The forward migration deleted the original shared (organization_id IS NULL)
-- rows and replaced each with one org-owned copy per organization, giving every
-- copy a fresh ``id``. The original row ids and the fact that a row was once
-- shared are not recorded anywhere, so the deleted rows cannot be recreated
-- faithfully -- and un-fanning the per-org copies would delete backends that
-- organizations may already have edited or pinned models to. Rolling back
-- therefore restores the ABILITY to have shared backends; it does not
-- manufacture one.
--
-- Concretely, after this down migration:
--   * ``organization_id`` accepts NULL again;
--   * the uniqueness constraint is again NULLS NOT DISTINCT, so at most one
--     shared row may exist per (engine, provider, name);
--   * the per-org copies created by 165 remain in place. They are ordinary
--     backends; nothing marks them as clones.
--
-- ``models.backend_id`` / ``requests.backend_id`` / ``tasks.backend_id`` rows
-- that the forward DELETE set to NULL stay NULL (the pre-delete pins are not
-- recorded), which is the same default-engine resolution the forward migration
-- relied on.
--
-- Re-running 165 after this rollback is safe: it fans out whatever shared rows
-- exist (none, unless someone inserts one) and re-applies the same constraints.
--
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

-- Restore a plain unique constraint name first, so the NULLS NOT DISTINCT
-- version replaces it under the same name and ON CONFLICT inference keeps
-- resolving (organization_id, engine, provider, name).
ALTER TABLE backends
    DROP CONSTRAINT IF EXISTS backends_organization_id_engine_provider_name_key;

ALTER TABLE backends
    ADD CONSTRAINT backends_organization_id_engine_provider_name_key
    UNIQUE NULLS NOT DISTINCT (organization_id, engine, provider, name);

ALTER TABLE backends
    ALTER COLUMN organization_id DROP NOT NULL;
