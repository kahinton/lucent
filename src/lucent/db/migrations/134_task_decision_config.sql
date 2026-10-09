-- Migration 134: Decision model steps as tasks
-- Adds a decision_config payload to tasks so a task can be executed by a
-- System One decision model (lucent.llm.decision /v1/systemone) instead of
-- a chat session. Fully additive — no existing data or behavior changes.
--
-- A task carrying decision_config must pair with agent_type
-- 'decision-step' (seeded built-in agent definition) and is dispatched by
-- the daemon's tracked-task dispatcher without a chat session: the state
-- is built from the parent request + completed sibling task results, the
-- configured questions are answered once, and the answers are stored as
-- result / result_structured.

ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS decision_config JSONB;

COMMENT ON COLUMN tasks.decision_config IS
  'Decision-step payload: {questions: [{name, type: noul|choice|score, instructions, criteria?}], state?: {..literal..}, model?: id, threshold_policy?: {questions: {name: {min: float}}, on_fail: fail_task|needs_review}}';