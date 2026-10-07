-- Down for 129: recreate `memory_access_grants` (100's DDL) and project the
-- read clearances that 129/+app-code created back into it. The daemon ->
-- first-owner reassignment is NOT undone (heuristic-only, same trade-off as
-- the 127/128 downs); manual owner reverts keep correct clearances via the
-- 123 transfer trigger.

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

-- Project read clearances back into grant rows (org/user/group read grants).
-- Owner clearances are excluded by role. A former daemon row made private to
-- its owner projects no org grant; 129 §6's daemon self-read clearances
-- project back as user grants naming the daemon service identity.
INSERT INTO memory_access_grants
    (memory_id, organization_id, grantee_type, grantee_user_id, grantee_group_id, created_by)
SELECT
    m.id,
    m.organization_id,
    CASE c.principal_type
        WHEN 'org' THEN 'organization'
        ELSE c.principal_type
    END AS grantee_type,
    CASE c.principal_type WHEN 'user' THEN c.principal_id END AS grantee_user_id,
    CASE c.principal_type WHEN 'group' THEN c.principal_id END AS grantee_group_id,
    c.granted_by
FROM memories m
JOIN auth_clearances c ON c.auth_id = m.auth_id
WHERE c.role = 'read'
  AND m.organization_id IS NOT NULL
ON CONFLICT DO NOTHING;