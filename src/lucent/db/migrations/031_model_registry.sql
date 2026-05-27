-- Migration 031: Model Registry
-- Moves the hardcoded model list into the database so admins can
-- enable, disable, add, and remove models via the UI/API.

CREATE TABLE IF NOT EXISTS models (
    id              VARCHAR(64) PRIMARY KEY,
    provider        VARCHAR(32) NOT NULL,         -- anthropic, openai, google
    name            VARCHAR(128) NOT NULL,        -- human-readable display name
    category        VARCHAR(32) NOT NULL DEFAULT 'general',  -- general, fast, reasoning, agentic, visual
    api_model_id    VARCHAR(128) DEFAULT '',      -- provider API model ID for LangChain
    context_window  INTEGER DEFAULT 0,            -- context window in tokens (0 = unknown)
    supports_tools  BOOLEAN DEFAULT true,
    supports_vision BOOLEAN DEFAULT false,
    notes           TEXT DEFAULT '',
    tags            TEXT[] DEFAULT '{}',
    is_enabled      BOOLEAN DEFAULT true,         -- admin toggle
    organization_id UUID REFERENCES organizations(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_models_provider ON models(provider);
CREATE INDEX IF NOT EXISTS idx_models_enabled ON models(is_enabled);
CREATE INDEX IF NOT EXISTS idx_models_org ON models(organization_id);

-- Seed with the current hardcoded models
INSERT INTO models (id, provider, name, category, api_model_id, supports_vision, notes, tags) VALUES
    ('gpt-4.1', 'openai', 'GPT-4.1', 'general', 'gpt-4.1', true, 'General-purpose coding and writing. Fast, accurate code completions.', '{"coding","writing","general"}'),
    ('gpt-5-mini', 'openai', 'GPT-5 mini', 'general', 'gpt-5-mini', true, 'Smaller GPT-5 variant. Good balance of speed and capability.', '{"coding","general"}'),
    ('gpt-5.1', 'openai', 'GPT-5.1', 'reasoning', 'gpt-5.1', true, 'Strong reasoning model with vision support.', '{"reasoning","coding"}'),
    ('gpt-5.1-codex', 'openai', 'GPT-5.1 Codex', 'agentic', 'gpt-5.1-codex', false, 'Agentic coding model for multi-step tasks.', '{"agentic","coding"}'),
    ('gpt-5.1-codex-max', 'openai', 'GPT-5.1 Codex Max', 'agentic', 'gpt-5.1-codex-max', false, 'Extended context and capability Codex variant.', '{"agentic","coding"}'),
    ('gpt-5.1-codex-mini', 'openai', 'GPT-5.1 Codex Mini', 'fast', 'gpt-5.1-codex-mini', false, 'Lighter Codex model for simpler agentic tasks.', '{"agentic","fast"}'),
    ('gpt-5.2', 'openai', 'GPT-5.2', 'reasoning', 'gpt-5.2', true, 'Advanced reasoning with vision.', '{"reasoning","coding"}'),
    ('gpt-5.2-codex', 'openai', 'GPT-5.2 Codex', 'agentic', 'gpt-5.2-codex', false, 'Agentic coding with improved planning.', '{"agentic","coding"}'),
    ('gpt-5.3-codex', 'openai', 'GPT-5.3 Codex', 'agentic', 'gpt-5.3-codex', false, 'Frontier agentic model. Excellent multi-step task execution.', '{"agentic","coding","frontier"}'),
    ('gpt-5.4', 'openai', 'GPT-5.4', 'reasoning', 'gpt-5.4', true, 'Top-tier reasoning model.', '{"reasoning","frontier"}'),
    ('claude-haiku-4.5', 'anthropic', 'Claude Haiku 4.5', 'fast', 'claude-haiku-4-5-20260301', false, 'Fastest Anthropic model. Good for simple tasks and memory maintenance.', '{"fast","lightweight"}'),
    ('claude-opus-4.5', 'anthropic', 'Claude Opus 4.5', 'reasoning', 'claude-opus-4-5-20260301', false, 'Previous generation flagship. Strong reasoning.', '{"reasoning","coding"}'),
    ('claude-opus-4.6', 'anthropic', 'Claude Opus 4.6', 'reasoning', 'claude-opus-4-6-20260301', false, 'Current flagship. Highest reasoning capability.', '{"reasoning","frontier","reflection"}'),
    ('claude-opus-4.6-fast', 'anthropic', 'Claude Opus 4.6 Fast', 'reasoning', 'claude-opus-4-6-fast-20260301', false, 'Opus-level reasoning with reduced latency.', '{"reasoning","fast"}'),
    ('claude-sonnet-4.0', 'anthropic', 'Claude Sonnet 4.0', 'general', 'claude-sonnet-4-0-20260301', false, 'Balanced performance and cost.', '{"coding","general"}'),
    ('claude-sonnet-4.5', 'anthropic', 'Claude Sonnet 4.5', 'general', 'claude-sonnet-4-5-20260301', false, 'Improved Sonnet with better tool use.', '{"coding","general"}'),
    ('claude-sonnet-4.6', 'anthropic', 'Claude Sonnet 4.6', 'general', 'claude-sonnet-4-6-20260301', false, 'Latest Sonnet. Best default for coding tasks.', '{"coding","general","default"}'),
    ('gemini-2.5-pro', 'google', 'Gemini 2.5 Pro', 'reasoning', 'gemini-2.5-pro', false, 'Strong research and analysis model.', '{"reasoning","research"}'),
    ('gemini-3-flash', 'google', 'Gemini 3 Flash', 'fast', 'gemini-3-flash', false, 'Ultra-fast Google model for lightweight tasks.', '{"fast","lightweight"}'),
    ('gemini-3-pro', 'google', 'Gemini 3 Pro', 'reasoning', 'gemini-3-pro', false, 'High-capability Google model for complex tasks.', '{"reasoning","research"}'),
    ('gemini-3-pro-preview', 'google', 'Gemini 3 Pro Preview', 'reasoning', 'gemini-3-pro-preview', false, 'Preview variant of Gemini 3 Pro.', '{"reasoning","preview"}'),
    ('gemini-3.1-pro', 'google', 'Gemini 3.1 Pro', 'reasoning', 'gemini-3.1-pro', false, 'Effective edit-then-test loops with high tool precision.', '{"reasoning","agentic","tools","frontier"}')
ON CONFLICT (id) DO NOTHING;
