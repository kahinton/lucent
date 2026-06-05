-- Migration 084: First-class user interactions from Lucent/daemon/workflows
-- Stores proactive messages, clarification requests, workflow handoffs, and
-- user replies with durable context references so the daemon can pause and
-- resume work without reconstructing context from scratch.

CREATE TABLE IF NOT EXISTS user_interactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,

    source VARCHAR(32) NOT NULL DEFAULT 'daemon',
    interaction_type VARCHAR(32) NOT NULL DEFAULT 'message',
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    priority VARCHAR(8) NOT NULL DEFAULT 'medium',

    title VARCHAR(256) NOT NULL,
    body TEXT NOT NULL,
    response_prompt TEXT,
    requires_response BOOLEAN NOT NULL DEFAULT false,
    dedupe_key TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    first_response_at TIMESTAMPTZ,
    last_response_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    dismissed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_user_interactions_source CHECK (
        source IN ('daemon', 'workflow', 'task', 'request', 'integration', 'system', 'human')
    ),
    CONSTRAINT ck_user_interactions_type CHECK (
        interaction_type IN (
            'message', 'clarification', 'review', 'decision',
            'workflow_output', 'handoff'
        )
    ),
    CONSTRAINT ck_user_interactions_status CHECK (
        status IN ('open', 'waiting_on_user', 'responded', 'resolved', 'dismissed')
    ),
    CONSTRAINT ck_user_interactions_priority CHECK (
        priority IN ('low', 'medium', 'high', 'urgent')
    ),
    CONSTRAINT ck_user_interactions_title_nonempty CHECK (length(trim(title)) > 0),
    CONSTRAINT ck_user_interactions_body_nonempty CHECK (length(trim(body)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_user_interactions_org_user_status_recent
    ON user_interactions(organization_id, user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_interactions_org_status_recent
    ON user_interactions(organization_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_interactions_type_recent
    ON user_interactions(organization_id, interaction_type, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_user_interactions_open_dedupe
    ON user_interactions(organization_id, user_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL
      AND status IN ('open', 'waiting_on_user', 'responded');

CREATE TABLE IF NOT EXISTS user_interaction_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interaction_id UUID NOT NULL REFERENCES user_interactions(id) ON DELETE CASCADE,
    sender_type VARCHAR(16) NOT NULL,
    sender_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_user_interaction_messages_sender CHECK (
        sender_type IN ('daemon', 'user', 'system')
    ),
    CONSTRAINT ck_user_interaction_messages_body_nonempty CHECK (length(trim(body)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_user_interaction_messages_thread
    ON user_interaction_messages(interaction_id, created_at, id);

CREATE TABLE IF NOT EXISTS user_interaction_references (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interaction_id UUID NOT NULL REFERENCES user_interactions(id) ON DELETE CASCADE,
    reference_type VARCHAR(32) NOT NULL,
    reference_id UUID,
    label VARCHAR(256),
    url TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_user_interaction_references_type CHECK (
        reference_type IN (
            'request', 'task', 'task_output', 'memory', 'workflow',
            'schedule_run', 'llm_session', 'url', 'other'
        )
    ),
    CONSTRAINT ck_user_interaction_references_identity CHECK (
        reference_id IS NOT NULL OR url IS NOT NULL OR label IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_user_interaction_refs_thread
    ON user_interaction_references(interaction_id, reference_type, created_at);
CREATE INDEX IF NOT EXISTS idx_user_interaction_refs_target
    ON user_interaction_references(reference_type, reference_id)
    WHERE reference_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS user_interaction_views (
    interaction_id UUID NOT NULL REFERENCES user_interactions(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    first_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (interaction_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_user_interaction_views_user_recent
    ON user_interaction_views(organization_id, user_id, last_viewed_at DESC);

COMMENT ON TABLE user_interactions IS
  'Proactive Lucent→user interactions: messages, clarification requests, workflow handoffs, and review prompts with durable context.';
COMMENT ON COLUMN user_interactions.dedupe_key IS
  'Optional producer-supplied key that prevents repeated open interactions for the same user/context.';
COMMENT ON TABLE user_interaction_references IS
  'Context attachments for an interaction, linking back to requests, memories, workflow runs, task outputs, sessions, or external URLs.';
