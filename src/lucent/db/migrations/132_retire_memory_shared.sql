-- 132: retire the memories.shared column.
--
-- Post-129 the column was only a cosmetic projection of "the memory carries a
-- ('read','org') clearance" — every writer of the column (create, set_shared,
-- grant_access, revoke_access) was simultaneously filing or removing exactly
-- that clearance, and the dev-DB projection drifted in 0 of 1,684 rows.
-- The clearance is now the single source of truth; readers that need the
-- badge/toggle state probe it (MemoryRepository.org_shared_ids / is_org_shared
-- on plain scoped connections) instead of reading a maintained copy.
--
-- Dropping the column takes its two dependent partial indexes
-- (idx_memories_org_shared from 004, idx_memories_org_shared_active from 010)
-- with it. The .down re-adds the column and backfills it from the clearances
-- before recreating both indexes, so the reversal is fully faithful for any
-- row that has a clearance at rollback time.

ALTER TABLE memories DROP COLUMN shared;

-- The COMMENT migration 100 put on the column dies with it (see .down).

COMMENT ON TABLE memories IS
    'Org-wide sharing is represented solely by (''read'',''org'') clearances '
    '(migrations 129/132); the legacy shared column is gone.';