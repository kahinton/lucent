-- Migration 162: database-level guard on memories.username attribution.
--
-- `memories.username` is attribution, not content: it identifies the principal
-- that owns the row (memories.user_id). The one application-side rule lives in
-- lucent.db.memory.resolve_memory_username:
--
--     user_id set   -> users.display_name, then users.email, then user_id::text
--     user_id unset -> a registered system principal ("System")
--
-- Every writer now goes through that helper, but the column itself had no
-- guard. A call path nobody audited could still write caller text — a chat
-- session title, a request name, a handoff subject — into the column. This
-- trigger makes that impossible at the table.
--
-- Allowed values for a row with an owner (user_id IS NOT NULL):
--   * exactly the owner's resolved identity (same fallback chain as the
--     helper, truncated to 255 chars like the helper does), or
--   * a registered system principal. The complete set is SYSTEM_MEMORY_PRINCIPALS
--     in src/lucent/db/memory.py: 'System' and 'Lucent Daemon'. (The per-org
--     daemon *user* row also has display_name 'Lucent Daemon', so daemon-owned
--     rows satisfy both arms.)
--
-- Rows with user_id IS NULL are deliberately exempt. They predate user
-- ownership, cannot be resolved automatically, and existing writers (legacy
-- imports and test fixtures) insert them with a pre-set label. Blocking that
-- shape would require guessing an owner; the guard leaves it alone instead.
-- Migration 163 (memory_username_backfill) repairs owned rows automatically
-- and reports the user_id IS NULL count via NOTICE.
--
-- Ordering with the data repair:
--   This migration validates NEW writes only — CREATE TRIGGER does not inspect
--   existing rows — so it applies cleanly on a database whose bad rows have
--   not been repaired yet. Migration 163 (memory_username_backfill) always
--   runs after it in the upgrade sequence, repairs the rows written before
--   this guard existed, and its corrected values satisfy the guard (the guard
--   stays enabled during the repair — see 163's header).
--
-- Idempotent: CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS.

CREATE OR REPLACE FUNCTION enforce_memories_username_integrity()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    resolved_identity TEXT;
BEGIN
    -- Unresolvable (legacy) rows: no owner to derive an identity from.
    IF NEW.user_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Registered system principals are legitimate attribution and are not
    -- derived from a users row. Keep in sync with SYSTEM_MEMORY_PRINCIPALS
    -- in src/lucent/db/memory.py.
    IF NEW.username IN ('System', 'Lucent Daemon') THEN
        RETURN NEW;
    END IF;

    -- Same chain as resolve_memory_username(): display_name -> email -> id,
    -- then [:255]. NULLIF (not btrim) intentionally matches the helper.
    SELECT left(
               COALESCE(
                   NULLIF(u.display_name, ''),
                   NULLIF(u.email, ''),
                   NEW.user_id::text
               ),
               255
           )
      INTO resolved_identity
      FROM users u
     WHERE u.id = NEW.user_id;

    -- FK memories.user_id -> users.id makes this unreachable on a normal
    -- write; fail closed if the constraint is ever bypassed.
    IF resolved_identity IS NULL THEN
        RAISE EXCEPTION
            'memories.username integrity: user_id % has no users row',
            NEW.user_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.username IS DISTINCT FROM resolved_identity THEN
        RAISE EXCEPTION
            'memories.username integrity: % is not the resolved identity % '
            'for user_id % (username is attribution derived from user_id)',
            NEW.username, resolved_identity, NEW.user_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS enforce_memories_username_integrity ON memories;
CREATE TRIGGER enforce_memories_username_integrity
    BEFORE INSERT OR UPDATE OF username, user_id
    ON memories
    FOR EACH ROW
    EXECUTE FUNCTION enforce_memories_username_integrity();

COMMENT ON FUNCTION enforce_memories_username_integrity() IS
    'Rejects memories.username that is neither a registered system principal '
    'nor the owner resolved identity (display_name -> email -> user_id). '
    'Rows with NULL user_id are exempt (unresolvable legacy shape).';
