-- Migration 100: Per-memory access grants
--
-- Replaces the binary private/org-shared model with additive grants for
-- organization-wide, individual-user, and group readers. The legacy shared
-- column remains as a compatibility projection for organization grants.

CREATE TABLE IF NOT EXISTS memory_access_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    grantee_type VARCHAR(16) NOT NULL CHECK (grantee_type IN ('organization', 'user', 'group')),
    grantee_user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    grantee_group_id UUID REFERENCES groups(id) ON DELETE CASCADE,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (grantee_type = 'organization' AND grantee_user_id IS NULL AND grantee_group_id IS NULL)
        OR (grantee_type = 'user' AND grantee_user_id IS NOT NULL AND grantee_group_id IS NULL)
        OR (grantee_type = 'group' AND grantee_user_id IS NULL AND grantee_group_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_access_grant_organization
    ON memory_access_grants(memory_id)
    WHERE grantee_type = 'organization';

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_access_grant_user
    ON memory_access_grants(memory_id, grantee_user_id)
    WHERE grantee_type = 'user';

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_access_grant_group
    ON memory_access_grants(memory_id, grantee_group_id)
    WHERE grantee_type = 'group';

CREATE INDEX IF NOT EXISTS idx_memory_access_grants_user
    ON memory_access_grants(organization_id, grantee_user_id, memory_id)
    WHERE grantee_type = 'user';

CREATE INDEX IF NOT EXISTS idx_memory_access_grants_group
    ON memory_access_grants(organization_id, grantee_group_id, memory_id)
    WHERE grantee_type = 'group';

CREATE INDEX IF NOT EXISTS idx_memory_access_grants_org
    ON memory_access_grants(organization_id, memory_id)
    WHERE grantee_type = 'organization';

INSERT INTO memory_access_grants (memory_id, organization_id, grantee_type, created_by)
SELECT id, organization_id, 'organization', user_id
FROM memories
WHERE shared IS TRUE
  AND organization_id IS NOT NULL
ON CONFLICT DO NOTHING;

ALTER TABLE memory_audit_log
    DROP CONSTRAINT IF EXISTS memory_audit_log_action_type_check;

ALTER TABLE memory_audit_log
    ADD CONSTRAINT memory_audit_log_action_type_check
    CHECK (action_type IN (
        'create', 'update', 'delete', 'restore', 'share', 'unshare',
        'hard_delete', 'system_cleanup', 'grant_access', 'revoke_access',
        'definition_approve', 'definition_create', 'definition_delete',
        'definition_grant', 'definition_reject', 'definition_revoke',
        'definition_update', 'secret_create', 'secret_delete', 'secret_read'
    ));

COMMENT ON TABLE memory_access_grants IS 'Read grants for memories. Owners and organization admins retain implicit access.';
COMMENT ON COLUMN memories.shared IS 'Legacy compatibility projection: true when the memory has an organization access grant.';