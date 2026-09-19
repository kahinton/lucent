-- Rollback for 115_provision_orphan_tables: drop the tables only if this
-- migration CREATED them on a fresh replay; on the live database (tables
-- pre-existing, no-op apply) rollback must destroy nothing. The runner
-- records the apply-time marker in schema_migrations.checksum:
--   'orphan-created:<sha256>'  -> this migration created the tables
--   '<sha256>' (no prefix)     -> tables pre-existed; no-op rollback
--
-- lucent_backup carries BYPASSRLS; the marker is read before any drop.
--
-- Guarded: on a fresh replayed database this migration CREATED the three
-- tables, so rollback drops them. On the live database it was a no-op
-- (tables pre-existed), and rollback must not destroy live data. We detect
-- which case we are in via a marker recorded at apply time.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM schema_migrations
        WHERE name = '115_provision_orphan_tables.sql'
          AND checksum LIKE 'orphan-created:%'
    ) THEN
        -- Fresh-replay case: we created them; safe to drop.
        DROP TABLE IF EXISTS resource_access_grants;
        DROP TABLE IF EXISTS requests;
        DROP TABLE IF EXISTS memories;
    ELSE
        -- Live case: tables pre-existed; drop nothing.
        RAISE NOTICE 'pre-existing tables; rollback is a no-op';
    END IF;
END
$$;