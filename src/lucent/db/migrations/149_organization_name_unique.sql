-- Migration 149: Enforce organization name uniqueness.
--
-- Organization names are the operator-facing handle for a tenant: the
-- hyperadmin API looks organizations up by name, the provisioning flow refuses
-- a duplicate name, and the local console lists them. That uniqueness has only
-- ever been checked in application code (OrganizationRepository.get_or_create),
-- which is a check-then-insert race: two concurrent signups with the same name
-- can both pass the check and create two tenants that look identical in every
-- list.
--
-- The index is case-insensitive because the names are for humans, and
-- "__lucent_system__" (the internal secret-storage org) is a normal row here —
-- no exception is needed.
--
-- This migration is deliberately defensive about pre-existing duplicates: it
-- renames rather than fails, so it can be applied to an instance that already
-- accumulated collisions. The oldest row keeps the name; later rows get a
-- " (n)" suffix. A hard failure here would block startup with no way out.

DO $$
DECLARE
    dup RECORD;
    suffix INTEGER;
    candidate TEXT;
BEGIN
    FOR dup IN
        SELECT id, name, created_at
        FROM organizations
        ORDER BY created_at, id
    LOOP
        -- First occurrence keeps its name; later ones get a numeric suffix.
        IF NOT EXISTS (
            SELECT 1 FROM organizations o
            WHERE LOWER(o.name) = LOWER(dup.name)
              AND (
                  o.created_at < dup.created_at
                  OR (o.created_at = dup.created_at AND o.id < dup.id)
              )
        ) THEN
            CONTINUE;
        END IF;

        suffix := 1;
        LOOP
            candidate := dup.name || ' (' || suffix || ')';
            EXIT WHEN NOT EXISTS (
                SELECT 1 FROM organizations WHERE LOWER(name) = LOWER(candidate)
            );
            suffix := suffix + 1;
        END LOOP;

        UPDATE organizations SET name = candidate WHERE id = dup.id;
        RAISE NOTICE 'Renamed duplicate organization % to %', dup.id, candidate;
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_name_unique
    ON organizations (LOWER(name));

COMMENT ON INDEX idx_organizations_name_unique IS
    'Organization names are unique per instance (case-insensitive). Enforced '
    'in the database so concurrent tenant creation cannot produce duplicates.';
