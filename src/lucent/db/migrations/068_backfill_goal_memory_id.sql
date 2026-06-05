-- Migration 068: Backfill goal_memory_id for open legacy requests
--
-- Migration 067 added structured goal columns. Any request created before
-- 067 only has its goal link in request_memories(relation='goal'). For
-- the planning-targets dedup to correctly treat those legacy rows as
-- "in flight for this goal", we backfill goal_memory_id from the link.
--
-- We leave goal_milestone_index NULL because we no longer parse titles.
-- A NULL milestone index on an open request is treated by the planning
-- query as "advances the whole goal", which blocks all per-milestone
-- planning for that goal until the legacy request completes. That's the
-- correct conservative behavior during the transition.
--
-- Only open (non-terminal) requests are backfilled; completed/cancelled
-- rows don't matter for planning.

UPDATE requests r
SET goal_memory_id = rm.memory_id
FROM request_memories rm
JOIN memories m ON m.id = rm.memory_id
WHERE rm.request_id = r.id
  AND rm.relation = 'goal'
  AND m.type = 'goal'
  AND m.deleted_at IS NULL
  AND r.goal_memory_id IS NULL
  AND r.status NOT IN ('completed', 'cancelled');
