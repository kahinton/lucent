-- Migration 135: Fusion configs on tasks (chat candidates + decision arbiter)
-- Adds a fusion_config payload to chat tasks so the daemon can run the task
-- against several candidate chat models inside one dispatch, score every
-- candidate with a System One decision model (one /v1/systemone call), and
-- settle on a best candidate (best_of_n) or synthesize one merged answer
-- (synthesize), with an optional bounded refine pass. Fully additive — no
-- existing data or behavior changes.
--
-- Pairing rules (enforced server-side):
--   * fusion_config is mutually exclusive with decision_config.
--   * fusion_config requires a chat-family agent_type (never decision-step
--     and never server_function / user_interaction pseudo-agents).
--   * output_contract IS allowed with fusion_config — unlike decision_config
--     — because the winner / synthesis result still goes through the normal
--     output-contract validation and repair tail.

ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS fusion_config JSONB;

COMMENT ON COLUMN tasks.fusion_config IS
  'Fusion payload: {mode: best_of_n|synthesize, candidates: {models: [..]} | {count: n}, arbiter?: {model?, questions: [<decision questions>], threshold_policy?}, synthesis?: {model?}, refine?: {model?, max_refine_passes}}; gated by the fusion.* runtime settings (default off)';