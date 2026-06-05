-- Secret storage table for encrypted secrets with ownership.
-- Follows the same ownership model as agent_definitions, skill_definitions, etc.

CREATE TABLE secrets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key VARCHAR(256) NOT NULL,
    encrypted_value BYTEA NOT NULL,
    owner_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    owner_group_id UUID REFERENCES groups(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_secret_owner CHECK (
        owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL
    ),
    UNIQUE(key, organization_id, owner_user_id),
    UNIQUE(key, organization_id, owner_group_id)
);

CREATE INDEX idx_secrets_org ON secrets(organization_id);
CREATE INDEX idx_secrets_owner_user ON secrets(organization_id, owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX idx_secrets_owner_group ON secrets(organization_id, owner_group_id) WHERE owner_group_id IS NOT NULL;
