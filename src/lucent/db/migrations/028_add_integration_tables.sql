-- Migration 028: Integration tables for Slack & Discord
-- Supports: integrations config, user identity linking, pairing code challenges
-- Design: memory 2c958fa1 (approved 2026-03-18)

-- Integrations: one per org+type+workspace, stores encrypted credentials
CREATE TABLE IF NOT EXISTS integrations (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    type                  TEXT NOT NULL CHECK (type IN ('slack', 'discord')),
    status                TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'disabled', 'revoked', 'deleted')),
    encrypted_config      BYTEA NOT NULL,
    config_version        INTEGER NOT NULL DEFAULT 1,
    external_workspace_id TEXT,
    allowed_channels      JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by            UUID NOT NULL REFERENCES users(id),
    updated_by            UUID REFERENCES users(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at           TIMESTAMPTZ,
    revoked_at            TIMESTAMPTZ,
    revoke_reason         TEXT
);

-- Only one active integration per org+type+workspace
CREATE UNIQUE INDEX IF NOT EXISTS idx_integrations_active_unique
    ON integrations (organization_id, type, external_workspace_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_integrations_org_status
    ON integrations (organization_id, status);

-- User links: maps external identities to Lucent users (6-state lifecycle)
CREATE TABLE IF NOT EXISTS user_links (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    integration_id           UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider                 TEXT NOT NULL CHECK (provider IN ('slack', 'discord')),
    external_user_id         TEXT NOT NULL,
    external_workspace_id    TEXT,
    status                   TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'active', 'revoked',
                                               'superseded', 'orphaned', 'disabled')),
    verification_method      TEXT NOT NULL
                             CHECK (verification_method IN ('pairing_code', 'admin', 'oauth')),
    superseded_by            UUID REFERENCES user_links(id),
    linked_at                TIMESTAMPTZ,
    revoked_at               TIMESTAMPTZ,
    revoked_by               UUID REFERENCES users(id),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Identity resolution: lookup active link by external identity tuple
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_links_active_identity
    ON user_links (provider, external_user_id, external_workspace_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_user_links_integration_status
    ON user_links (integration_id, status);

CREATE INDEX IF NOT EXISTS idx_user_links_user_status
    ON user_links (user_id, status);

-- Pairing challenges: 128-bit codes for identity verification
CREATE TABLE IF NOT EXISTS pairing_challenges (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_id         UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    user_id                UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash              TEXT NOT NULL,          -- bcrypt hash of 128-bit pairing code
    expires_at             TIMESTAMPTZ NOT NULL,   -- TTL enforcement
    attempt_count          INTEGER NOT NULL DEFAULT 0,
    max_attempts           INTEGER NOT NULL DEFAULT 5,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'used', 'expired', 'exhausted')),
    claimed_by_external_id TEXT,                   -- external user who redeemed the code
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rate limit queries: pending challenges per user
CREATE INDEX IF NOT EXISTS idx_pairing_challenges_user_pending
    ON pairing_challenges (user_id, created_at)
    WHERE status = 'pending';

-- Cleanup: find expired pending challenges
CREATE INDEX IF NOT EXISTS idx_pairing_challenges_expiry
    ON pairing_challenges (expires_at)
    WHERE status = 'pending';
