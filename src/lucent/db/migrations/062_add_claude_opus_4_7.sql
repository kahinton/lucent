-- Migration 062: Add Claude Opus 4.7 model entry
-- Adds the new Anthropic Opus 4.7 model to the DB-backed model registry.
-- Uses upsert so existing deployments can rerun safely.

INSERT INTO models (
    id,
    provider,
    name,
    category,
    api_model_id,
    context_window,
    supports_tools,
    supports_vision,
    notes,
    tags
) VALUES (
    'claude-opus-4.7',
    'anthropic',
    'Claude Opus 4.7',
    'reasoning',
    'claude-opus-4-7-20260416',
    200000,
    true,
    false,
    'Latest Anthropic flagship. Default for frontier reasoning and agentic workflows.',
    '{"default","reasoning","frontier","agentic","reflection"}'
)
ON CONFLICT (id) DO UPDATE SET
    provider = EXCLUDED.provider,
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    api_model_id = EXCLUDED.api_model_id,
    context_window = EXCLUDED.context_window,
    supports_tools = EXCLUDED.supports_tools,
    supports_vision = EXCLUDED.supports_vision,
    notes = EXCLUDED.notes,
    tags = EXCLUDED.tags,
    updated_at = NOW();
