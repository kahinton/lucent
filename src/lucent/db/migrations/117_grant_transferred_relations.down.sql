-- Rollback for 117_grant_transferred_relations: step the wave's grant
-- coverage back to explicit full-owner entries (function-preserving).
--
-- HAZARD THIS DOWN AVOIDS (pgembed scratch, live-verified 2026-09-17):
-- a plain `REVOKE ALL ON <owned-relation> FROM <owner>` leaves the ACL
-- array EMPTY, and the owner is then genuinely "permission denied" on its
-- own table — REVOKE ALL does not fall back to implicit owner privileges.
-- 114's own down file survives blanket revokes only because REASSIGN
-- OWNED + DROP ROLE consume the role immediately after; a standalone 117
-- rollback must leave the pool role able to work.
--
-- Per lucent_app-owned relation this down therefore:
--   1. REVOKEs the exact DML/usage set 117 granted;
--   2. GRANT ALL PRIVILEGES restores an explicit full-owner entry.
-- The resulting entry (lucent_app=arwdDxtm) equals what a PostgreSQL 17
-- ownership transfer materializes on transferred relations, and is a
-- functional superset of the born-owned state whose ACL PostgreSQL may
-- elide (acl=NULL) when only the self-grant ADP applies. The server keeps
-- working throughout; only the DML-set-only shape of 117's entries
-- regresses (REFERENCES/TRIGGER bits appear).
--
-- In the full-wave rollback order (117 -> 116 -> 115 -> 114), 114.down's
-- REASSIGN + REVOKE ALL + DROP ROLE consumes these entries: nothing
-- lucent_app-owned survives to the pre-wave end state.
-- Idempotent: no-op when no lucent_app-owned relations exist (pre-cutover
-- live database) or the entries already match.

DO $$
DECLARE
    obj record;
BEGIN
    -- Tables (incl. partitioned roots), views, matviews owned by
    -- lucent_app.
    FOR obj IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'v', 'm')
          AND pg_get_userbyid(c.relowner) = 'lucent_app'
    LOOP
        EXECUTE format('REVOKE INSERT, SELECT, UPDATE, DELETE ON %I FROM lucent_app',
                       obj.relname);
        EXECUTE format('GRANT ALL PRIVILEGES ON %I TO lucent_app',
                       obj.relname);
    END LOOP;

    -- Sequences owned by lucent_app (GRANT ALL = USAGE, SELECT, UPDATE).
    FOR obj IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'S'
          AND pg_get_userbyid(c.relowner) = 'lucent_app'
    LOOP
        EXECUTE format('REVOKE USAGE, SELECT ON %I FROM lucent_app',
                       obj.relname);
        EXECUTE format('GRANT ALL PRIVILEGES ON %I TO lucent_app',
                       obj.relname);
    END LOOP;
END
$$;