-- Migration 108: Persist provider-reported LLM token usage per model call.
-- Token values are recorded as immutable usage events rather than derived from
-- transcript text, so reporting remains accurate across providers and tool rounds.

CREATE TABLE IF NOT EXISTS llm_token_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    session_id UUID REFERENCES llm_sessions(id) ON DELETE CASCADE,
    turn_id UUID,
    message_id UUID REFERENCES llm_messages(id) ON DELETE SET NULL,
    model VARCHAR(128) NOT NULL,
    engine VARCHAR(32) NOT NULL,
    provider_call_id TEXT,
    input_tokens BIGINT NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens BIGINT NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens BIGINT NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    reasoning_tokens BIGINT NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    provider_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_token_usage_org_created
    ON llm_token_usage (organization_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_token_usage_org_user_model_created
    ON llm_token_usage (organization_id, user_id, model, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_token_usage_session_turn
    ON llm_token_usage (session_id, turn_id) WHERE session_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_llm_token_usage_provider_call
    ON llm_token_usage (session_id, provider_call_id)
    WHERE session_id IS NOT NULL AND provider_call_id IS NOT NULL;