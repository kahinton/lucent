-- Migration 059: Enterprise credential management
-- Adds encrypted credential storage, OAuth state tracking, and scope enforcement
-- for per-user and per-agent tool connectivity.

CREATE TABLE IF NOT EXISTS enterprise_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    integration_type TEXT NOT NULL
        CHECK (integration_type IN ('github', 'slack', 'jira', 'custom')),
    credential_kind TEXT NOT NULL
        CHECK (credential_kind IN ('oauth2', 'api_key', 'service_account')),

    scope_type TEXT NOT NULL
        CHECK (scope_type IN ('user', 'agent')),
    owner_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    owner_agent_id UUID REFERENCES agent_definitions(id) ON DELETE CASCADE,

    display_name TEXT NOT NULL,
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,

    encrypted_secret_payload BYTEA NOT NULL,
    encrypted_metadata BYTEA,

    access_token_expires_at TIMESTAMPTZ,
    refresh_token_expires_at TIMESTAMPTZ,
    last_refreshed_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,

    refresh_token_version INTEGER NOT NULL DEFAULT 1,
    token_rotated_at TIMESTAMPTZ,

    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked', 'expired')),

    created_by UUID NOT NULL REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_credential_owner_exclusive CHECK (
        ((owner_user_id IS NOT NULL)::int + (owner_agent_id IS NOT NULL)::int) = 1
    ),
    CONSTRAINT chk_credential_scope_user CHECK (
        scope_type != 'user' OR owner_user_id IS NOT NULL
    ),
    CONSTRAINT chk_credential_scope_agent CHECK (
        scope_type != 'agent' OR owner_agent_id IS NOT NULL
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_enterprise_credentials_unique_active
ON enterprise_credentials (
    organization_id,
    integration_type,
    scope_type,
    COALESCE(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(owner_agent_id, '00000000-0000-0000-0000-000000000000'::uuid),
    lower(display_name)
)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_enterprise_credentials_org_scope
ON enterprise_credentials (organization_id, scope_type, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_enterprise_credentials_owner_user
ON enterprise_credentials (organization_id, owner_user_id, status)
WHERE owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_enterprise_credentials_owner_agent
ON enterprise_credentials (organization_id, owner_agent_id, status)
WHERE owner_agent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_enterprise_credentials_expiry
ON enterprise_credentials (access_token_expires_at)
WHERE status = 'active' AND access_token_expires_at IS NOT NULL;


CREATE TABLE IF NOT EXISTS oauth2_state_challenges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    provider TEXT NOT NULL
        CHECK (provider IN ('github', 'slack', 'jira')),

    state_hash TEXT NOT NULL UNIQUE,
    pkce_verifier TEXT,
    redirect_uri TEXT NOT NULL,
    requested_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,

    scope_type TEXT NOT NULL CHECK (scope_type IN ('user', 'agent')),
    owner_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    owner_agent_id UUID REFERENCES agent_definitions(id) ON DELETE CASCADE,

    created_by UUID NOT NULL REFERENCES users(id),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_oauth_state_owner_exclusive CHECK (
        ((owner_user_id IS NOT NULL)::int + (owner_agent_id IS NOT NULL)::int) = 1
    ),
    CONSTRAINT chk_oauth_state_scope_user CHECK (
        scope_type != 'user' OR owner_user_id IS NOT NULL
    ),
    CONSTRAINT chk_oauth_state_scope_agent CHECK (
        scope_type != 'agent' OR owner_agent_id IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_oauth2_state_active
ON oauth2_state_challenges (organization_id, provider, expires_at)
WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_oauth2_state_owner_user
ON oauth2_state_challenges (organization_id, owner_user_id, expires_at)
WHERE owner_user_id IS NOT NULL AND consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_oauth2_state_owner_agent
ON oauth2_state_challenges (organization_id, owner_agent_id, expires_at)
WHERE owner_agent_id IS NOT NULL AND consumed_at IS NULL;
