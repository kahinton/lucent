-- Migration 133: Explicit model classification (model_class)
-- Adds a hard classification boundary between standard chat/language models
-- and System One decision models (Jev-style choice/noul/score, served
-- locally by Ollama 0.35+ via /v1/systemone). ``category`` stays a
-- task-flavor label; ``model_class`` is what routing and selection treat
-- as authoritative so decision models never enter chat-model selection,
-- daemon dispatch, or the LangChain chat runtime registry.

ALTER TABLE models
    ADD COLUMN model_class VARCHAR(16) NOT NULL DEFAULT 'language';

COMMENT ON COLUMN models.model_class IS
    'Explicit classification: ''language'' (chat LLM) or ''decision'' (System One decision model)';

CREATE INDEX IF NOT EXISTS idx_models_model_class ON models(model_class);