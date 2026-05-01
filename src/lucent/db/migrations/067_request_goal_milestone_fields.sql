-- Migration 067: First-class goal milestone fields on requests
--
-- Until now, the link between a request and the goal-milestone it advances
-- has been inferred from two unreliable sources:
--   1) request_memories(relation='goal') — late-bound link added after the
--      request exists, easy to bypass
--   2) Title parsing — looking for 'M3:' / 'Phase 3:' / 'Milestone 3' in the
--      title string, which is fragile and forces a naming convention on the
--      planner
--
-- Both have produced bugs (wrong-milestone proposals, late-link bypass of
-- goal-state validation, duplicate work). This migration replaces the
-- inferred relationship with two structured columns so validation,
-- planning-target queries, and on-completion milestone updates can be
-- expressed as direct SQL.
--
-- request_memories is unaffected — it remains the right place to attach
-- arbitrary context memories (technical/experience/etc) to a request.
-- Goals are special and now get their own dedicated columns.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS goal_memory_id UUID NULL
        REFERENCES memories(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS goal_milestone_index INTEGER NULL;

-- A request that names a milestone MUST also name the goal it belongs to.
-- (The reverse is allowed: a goal-only request advances the goal as a
-- whole, e.g. for goals that don't use milestones.)
ALTER TABLE requests
    DROP CONSTRAINT IF EXISTS requests_goal_milestone_requires_goal,
    ADD CONSTRAINT requests_goal_milestone_requires_goal
        CHECK (goal_milestone_index IS NULL OR goal_memory_id IS NOT NULL);

-- Milestone indexes are 1-based and bounded by reasonable list sizes.
ALTER TABLE requests
    DROP CONSTRAINT IF EXISTS requests_goal_milestone_index_positive,
    ADD CONSTRAINT requests_goal_milestone_index_positive
        CHECK (goal_milestone_index IS NULL OR goal_milestone_index >= 1);

-- Index for the planning-target "is this milestone already in flight?" query.
-- Partial: only index rows that actually point at a milestone, since most
-- requests don't.
CREATE INDEX IF NOT EXISTS idx_requests_goal_milestone
    ON requests (goal_memory_id, goal_milestone_index)
    WHERE goal_memory_id IS NOT NULL;

COMMENT ON COLUMN requests.goal_memory_id IS
    'When this request advances a specific goal memory, the goal''s id. '
    'NULL for non-goal-driven requests (ad-hoc work, scheduled jobs, etc). '
    'Distinct from request_memories: that table holds arbitrary context '
    'attachments; this column is the structured "this request advances goal X" link.';

COMMENT ON COLUMN requests.goal_milestone_index IS
    '1-based index into goal_memory.metadata.milestones. NULL means the request '
    'advances the goal as a whole (no milestones array, or a top-level effort). '
    'Validation at create-time ensures the indexed milestone is currently '
    '''active''. On request completion, the milestone is automatically marked '
    '''completed'' on the goal memory.';
