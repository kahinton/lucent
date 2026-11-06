-- Migration 163: repair memories.username attribution on already-damaged rows.
--
-- Migration 162 made bad attribution impossible for NEW writes, but it does
-- not inspect existing rows: an instance upgrading to this code carries its
-- bad rows forward untouched. This migration repairs them automatically, so
-- the upgrade path needs no manual SQL step. It is a no-op where the data is
-- already clean.
--
-- The accepted form — identical to migration 162's guard trigger and to
-- lucent.db.memory.resolve_memory_username(), and written here exactly — is
-- the owner reached through memories.user_id = users.id:
--
--     SELECT left(
--                COALESCE(
--                    NULLIF(u.display_name, ''),
--                    NULLIF(u.email, ''),
--                    m.user_id::text
--                ),
--                255
--            )
--       FROM users u
--      WHERE u.id = m.user_id
--
-- display_name first, then email, then the user_id text form; empty strings
-- are skipped via NULLIF (not btrim, matching the helper), the result is
-- truncated to 255 chars like the helper. The comparison is case-sensitive on
-- purpose: the guard compares with IS DISTINCT FROM, so a case-only difference
-- ('kahinton' vs 'Kahinton') is a mismatch and is repaired too. The identity
-- is derived from the users row only — never from memory content, titles, or
-- session text.
--
-- Scope:
--   * Rows with an owner (user_id IS NOT NULL) whose username is not a
--     registered system principal ('System', 'Lucent Daemon' — the complete
--     SYSTEM_MEMORY_PRINCIPALS set in src/lucent/db/memory.py) and is not the
--     resolved identity. NULL usernames on owned rows are repaired as well:
--     the migration-162 guard rejects that shape on every future write.
--   * Rows with user_id IS NULL are left untouched. No owner means no
--     resolvable identity and guessing one is not an option; the row count is
--     reported via RAISE NOTICE instead. (Rows there already labelled with a
--     system principal are legitimate legacy attribution.)
--
-- What the repair preserves:
--   * version is not in the SET list. A data repair is not a content revision
--     and must not bump the row version (or create an audit-log entry).
--   * updated_at is preserved byte-identical. The base-schema stamper
--     update_memories_updated_at (migration 001, update_updated_at_column())
--     unconditionally assigns NEW.updated_at = NOW() on every UPDATE and
--     fires after the guard in trigger-name order, so it overwrites any value
--     the SET clause could specify. Migration 095 solved the same problem
--     with ALTER TABLE ... DISABLE TRIGGER — exactly the trigger-disable
--     hazard this migration refuses to reintroduce, because a crash or an
--     operator error between DISABLE and ENABLE leaves the guard silently
--     off. Instead the migration creates a temporary BEFORE UPDATE trigger
--     that sorts alphabetically after the stamper ('z...' >= 'u...', and
--     PostgreSQL fires same-event triggers in trigger-name order) and simply
--     restores NEW.updated_at from OLD.updated_at, then drops it, all inside
--     the transaction the migration runner commits:
--
--         enforce_memories_username_integrity   162 guard: validates the write
--         update_memories_updated_at            001 stamper: stamps NOW()
--         z_backfill_163_preserve_updated_at    this migration: restores value
--
--     No existing trigger is disabled, dropped, or altered at any point, and
--     the temporary trigger outlives neither a commit nor a rollback of this
--     migration.
--
-- Trigger safety during the repair: the migration-162 guard stays enabled
-- throughout and validates every repaired row. It accepts the resolved
-- identity by construction; if it ever rejects one, its RAISE aborts the
-- migration and the whole transaction (temporary trigger included) rolls
-- back. No target row is ever silently skipped.
--
-- Bounds / locking: the repair is a single UPDATE over exactly the mismatched
-- rows — on a clean database that set is empty, and the count is reported via
-- RAISE NOTICE before anything is written. The temporary trigger takes SHARE
-- ROW EXCLUSIVE on memories and the UPDATE takes ROW EXCLUSIVE, both for this
-- transaction only — lighter than migration 095's ALTER TABLE ACCESS
-- EXCLUSIVE approach. Large tables pay one index-backed join pass; the
-- repaired-row count is whatever is actually wrong, not the table size.
--
-- Idempotent: the target predicate is `username IS DISTINCT FROM <resolved
-- identity>`, so a second run updates zero rows (the closing NOTICE always
-- reports 0 remaining). Re-running is safe and intended.
--
-- Fail-loud check: after the UPDATE, the same mismatch predicate is
-- re-counted; anything other than zero raises and aborts the migration.

CREATE TEMP TABLE _backfill_163_targets ON COMMIT DROP AS
SELECT m.id AS memory_id,
       m.username AS old_username,
       m.updated_at AS old_updated_at,
       left(
           COALESCE(
               NULLIF(u.display_name, ''),
               NULLIF(u.email, ''),
               m.user_id::text
           ),
           255
       ) AS resolved_username
  FROM memories m
  JOIN users u ON u.id = m.user_id
 WHERE m.user_id IS NOT NULL
   AND m.username IS DISTINCT FROM 'System'
   AND m.username IS DISTINCT FROM 'Lucent Daemon'
   AND m.username IS DISTINCT FROM
       left(
           COALESCE(
               NULLIF(u.display_name, ''),
               NULLIF(u.email, ''),
               m.user_id::text
           ),
           255
       );

DO $backfill_163$
DECLARE
    repair_count BIGINT;
    repaired_count BIGINT;
    unowned_rows BIGINT;
    unowned_needs_review BIGINT;
    remaining BIGINT;
BEGIN
    -- Report before writing anything: these counts describe exactly what this
    -- run will do.
    SELECT count(*) INTO repair_count FROM _backfill_163_targets;

    SELECT count(*),
           count(*) FILTER (
               WHERE username IS DISTINCT FROM 'System'
                 AND username IS DISTINCT FROM 'Lucent Daemon'
           )
      INTO unowned_rows, unowned_needs_review
      FROM memories
     WHERE user_id IS NULL;

    RAISE NOTICE 'migration 163: % owned row(s) to repair',
        repair_count;
    RAISE NOTICE 'migration 163: % row(s) with user_id IS NULL left untouched '
                 '(% still carry a non-system label and need manual review)',
        unowned_rows, unowned_needs_review;

    IF repair_count = 0 THEN
        RAISE NOTICE
            'migration 163: data already clean, nothing to do '
            '(a second run always reports 0)';

        -- Still install and remove the temporary trigger below? No: nothing
        -- will be updated, so there is nothing to preserve. Skip to the
        -- closing check.
    ELSE
        -- updated_at preservation, see the header. Lives and dies inside this
        -- migration's transaction.
        CREATE OR REPLACE FUNCTION migration_163_preserve_updated_at()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $backfill_163_fn$
        BEGIN
            NEW.updated_at := OLD.updated_at;
            RETURN NEW;
        END;
        $backfill_163_fn$;

        DROP TRIGGER IF EXISTS z_backfill_163_preserve_updated_at ON memories;
        CREATE TRIGGER z_backfill_163_preserve_updated_at
            BEFORE UPDATE ON memories
            FOR EACH ROW
            EXECUTE FUNCTION migration_163_preserve_updated_at();

        UPDATE memories m
           SET username = t.resolved_username
          FROM _backfill_163_targets t
         WHERE m.id = t.memory_id;

        GET DIAGNOSTICS repaired_count = ROW_COUNT;

        DROP TRIGGER z_backfill_163_preserve_updated_at ON memories;
        DROP FUNCTION migration_163_preserve_updated_at();

        RAISE NOTICE 'migration 163: repaired % row(s); before/after values are '
                     'in _backfill_163_targets until this transaction commits',
            repaired_count;
    END IF;

    -- Fail loud: the only way an owned row is left uncorrected below is
    -- something the repair did not anticipate.
    SELECT count(*) INTO remaining
      FROM memories m
      JOIN users u ON u.id = m.user_id
     WHERE m.user_id IS NOT NULL
       AND m.username IS DISTINCT FROM 'System'
       AND m.username IS DISTINCT FROM 'Lucent Daemon'
       AND m.username IS DISTINCT FROM
           left(
               COALESCE(
                   NULLIF(u.display_name, ''),
                   NULLIF(u.email, ''),
                   m.user_id::text
               ),
               255
           );

    IF remaining <> 0 THEN
        RAISE EXCEPTION
            'migration 163: % owned row(s) still mismatch the resolved identity '
            'after the repair (the migration-162 guard rejected a value); '
            'aborting without committing',
            remaining
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RAISE NOTICE
        'migration 163: repair complete, 0 owned rows still mismatch';
END;
$backfill_163$;
