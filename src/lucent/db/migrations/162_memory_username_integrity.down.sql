-- Down: remove the memories.username attribution guard (migration 162).
--
-- The trigger and its function were the only schema objects added, so dropping
-- both restores the previous behaviour exactly. No data was changed by the
-- forward migration, and repaired attribution rows (from migration 163)
-- remain valid without the guard.

DROP TRIGGER IF EXISTS enforce_memories_username_integrity ON memories;
DROP FUNCTION IF EXISTS enforce_memories_username_integrity();
