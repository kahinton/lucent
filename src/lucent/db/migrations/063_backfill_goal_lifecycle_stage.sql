-- Migration 063: Backfill lifecycle_stage for completed/abandoned goal memories
--
-- Problem: metadata.status and lifecycle_stage can drift apart for goal-type
-- memories.  An audit on 2026-04-18 found 16/21 goals with lifecycle_stage
-- still 'active' despite metadata.status indicating completion.  A manual
-- backfill was applied at the time; this migration ensures any environment
-- that hasn't received the fix gets it automatically on next deploy.
--
-- Mapping (goal memories only):
--   metadata.status IN ('completed','done','abandoned','cancelled') → archived
--   metadata.status IN ('active','paused')                         → active  (no-op, already default)
--
-- Going forward, the service layer (MemoryRepository.update / create) keeps
-- these in sync automatically.  This migration is a one-time catch-up.

UPDATE memories
SET lifecycle_stage = 'archived'
WHERE type = 'goal'
  AND lifecycle_stage = 'active'
  AND (metadata->>'status') IN ('completed', 'done', 'abandoned', 'cancelled')
  AND deleted_at IS NULL;
