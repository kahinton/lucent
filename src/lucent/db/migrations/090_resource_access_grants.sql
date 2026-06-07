-- Migration 090: Many-to-many resource access grants
--
-- Replaces the single-owner access model (a resource is owned by exactly one
-- user OR one group OR the whole org) with an explicit access-grant list so any
-- resource can be shared with an arbitrary SET of users AND groups. "Org-wide"
-- becomes a special principal ('org') in the same mechanism. See
-- docs/access-control.md.
--
-- Access now resolves as:
--   built-in (platform global)  OR  owner_user_id = requester (the manager)
--   OR  requester role in (admin, owner)
--   OR  a grant row matches the requester (their user, any of their groups, or
--       an 'org' grant = everyone in the organization).
--
-- The owner triple's owner_group_id and the implicit org-shared (both NULL)
-- states are migrated into explicit grant rows so there is a single, uniform
-- representation of "who can access this". owner_user_id is retained purely as
-- the MANAGER (who, besides admins, may edit the resource and its grants).

-- ── Grant table ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS resource_access_grants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    resource_type   VARCHAR(40) NOT NULL,
    -- TEXT (not UUID): most resources use UUID primary keys, but the global
    -- models catalog uses string ids (e.g. provider slugs). Storing as TEXT lets
    -- one uniform grant table cover every resource type.
    resource_id     TEXT NOT NULL,
    principal_type  VARCHAR(10) NOT NULL
        CHECK (principal_type IN ('user', 'group', 'org')),
    principal_id    UUID NOT NULL,
    granted_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_resource_access_grants
        UNIQUE (resource_type, resource_id, principal_type, principal_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_resource
    ON resource_access_grants(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_rag_principal
    ON resource_access_grants(principal_type, principal_id);
CREATE INDEX IF NOT EXISTS idx_rag_org
    ON resource_access_grants(organization_id);

COMMENT ON TABLE resource_access_grants IS
    'Many-to-many access grants. principal_type org grants access to the whole '
    'organization (principal_id = organization_id). Owner (owner_user_id) and '
    'admin/owner role have implicit access and are not stored here.';

-- ── Backfill: org-shared (instance, both owners NULL) → org grant ─────────
-- Each scoped resource table that currently expresses "shared with everyone"
-- as both owner columns NULL on an instance-scoped row.
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'agent', id::text, 'org', organization_id FROM agent_definitions
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'skill', id::text, 'org', organization_id FROM skill_definitions
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'mcp_server', id::text, 'org', organization_id FROM mcp_server_configs
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'hook', id::text, 'org', organization_id FROM hook_definitions
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'managed_tool', id::text, 'org', organization_id FROM managed_tool_definitions
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'sandbox_template', id::text, 'org', organization_id FROM sandbox_templates
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL
ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'workflow', id::text, 'org', organization_id FROM schedules
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL
ON CONFLICT DO NOTHING;

-- ── Backfill: group-owned → group grant, then clear owner_group_id ────────
-- Group ownership becomes an explicit group grant so access has one
-- representation. owner_group_id is no longer an access input.
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'agent', id::text, 'group', owner_group_id FROM agent_definitions
    WHERE owner_group_id IS NOT NULL ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'skill', id::text, 'group', owner_group_id FROM skill_definitions
    WHERE owner_group_id IS NOT NULL ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'mcp_server', id::text, 'group', owner_group_id FROM mcp_server_configs
    WHERE owner_group_id IS NOT NULL ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'hook', id::text, 'group', owner_group_id FROM hook_definitions
    WHERE owner_group_id IS NOT NULL ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'managed_tool', id::text, 'group', owner_group_id FROM managed_tool_definitions
    WHERE owner_group_id IS NOT NULL ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'sandbox_template', id::text, 'group', owner_group_id FROM sandbox_templates
    WHERE owner_group_id IS NOT NULL ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'workflow', id::text, 'group', owner_group_id FROM schedules
    WHERE owner_group_id IS NOT NULL ON CONFLICT DO NOTHING;
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT organization_id, 'secret', id::text, 'group', owner_group_id FROM secrets
    WHERE owner_group_id IS NOT NULL ON CONFLICT DO NOTHING;

UPDATE agent_definitions        SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;
UPDATE skill_definitions        SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;
UPDATE mcp_server_configs       SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;
UPDATE hook_definitions         SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;
UPDATE managed_tool_definitions SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;
UPDATE sandbox_templates        SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;
UPDATE schedules                SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;
UPDATE secrets                  SET owner_group_id = NULL WHERE owner_group_id IS NOT NULL;

-- ── Models: make the global catalog restrictable ─────────────────────────
-- Models are a platform-global catalog (organization_id IS NULL) and were
-- marked built-in (visible to everyone in every org). To support per-user /
-- per-group access (cost control + security) WITHOUT breaking today's access,
-- convert them from built-in to grant-governed and seed an 'org' grant for
-- every (organization, model) pair so every org keeps full access initially.
-- Admins then narrow access per org by removing the org grant and adding
-- specific user/group grants. Because the same model row is shared across orgs,
-- the access clause constrains grant matching by organization_id (org_param)
-- so one org's grants never leak a shared model to another org.
--
-- Global models (no org): grant to every organization.
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT o.id, 'model', m.id::text, 'org', o.id
FROM models m CROSS JOIN organizations o
WHERE m.organization_id IS NULL
ON CONFLICT DO NOTHING;

-- Org-specific (custom) models: grant to their owning org only.
INSERT INTO resource_access_grants (organization_id, resource_type, resource_id, principal_type, principal_id)
SELECT m.organization_id, 'model', m.id::text, 'org', m.organization_id
FROM models m
WHERE m.organization_id IS NOT NULL
ON CONFLICT DO NOTHING;

-- Drop built-in so access is governed purely by grants (the built-in branch
-- would otherwise bypass every restriction). Keep owner_user_id as-is.
UPDATE models SET scope = 'instance' WHERE scope = 'built-in';
