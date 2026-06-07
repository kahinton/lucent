-- Migration 089: Ownership triple for workflows (schedules) and models
-- Brings workflows and models into the unified access-control plane defined in
-- docs/access-control.md. Each gains the (scope, owner_user_id, owner_group_id)
-- triple so visibility resolves through AccessControlService like every other
-- access-controlled resource: built-in -> owner_user -> owner_group ->
-- org-shared -> admin/owner.
--
-- Runtime resolution paths (model lookup by id for an LLM call, the scheduler
-- loading due workflows) intentionally remain unscoped; only management and
-- listing surfaces apply the ownership predicate.

-- ── Workflows (schedules) ────────────────────────────────────────────────
ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'instance',
    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_group_id UUID REFERENCES groups(id) ON DELETE SET NULL;

ALTER TABLE schedules
    DROP CONSTRAINT IF EXISTS ck_schedules_scope,
    ADD CONSTRAINT ck_schedules_scope CHECK (scope IN ('instance', 'built-in')),
    DROP CONSTRAINT IF EXISTS ck_schedules_single_owner,
    ADD CONSTRAINT ck_schedules_single_owner
        CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL));

-- System-managed workflows are built-in (org-global, not personally owned).
UPDATE schedules SET scope = 'built-in', owner_user_id = NULL, owner_group_id = NULL
    WHERE is_system = true;

-- User-created workflows default to personal ownership by their creator.
UPDATE schedules SET owner_user_id = created_by
    WHERE is_system = false AND created_by IS NOT NULL AND owner_user_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_schedules_owner_user ON schedules(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_schedules_owner_group ON schedules(owner_group_id);
CREATE INDEX IF NOT EXISTS idx_schedules_org_shared
    ON schedules(organization_id, status)
    WHERE scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL;

-- ── Models ───────────────────────────────────────────────────────────────
ALTER TABLE models
    ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'instance',
    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_group_id UUID REFERENCES groups(id) ON DELETE SET NULL;

ALTER TABLE models
    DROP CONSTRAINT IF EXISTS ck_models_scope,
    ADD CONSTRAINT ck_models_scope CHECK (scope IN ('instance', 'built-in')),
    DROP CONSTRAINT IF EXISTS ck_models_single_owner,
    ADD CONSTRAINT ck_models_single_owner
        CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL));

-- All pre-existing models are the org-global registry: mark them built-in so
-- they remain visible to everyone. New models created through the UI default to
-- 'instance' and may be personally or group owned.
UPDATE models SET scope = 'built-in', owner_user_id = NULL, owner_group_id = NULL;

CREATE INDEX IF NOT EXISTS idx_models_owner_user ON models(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_models_owner_group ON models(owner_group_id);

COMMENT ON COLUMN schedules.scope IS 'built-in (system/org-global) or instance (owned/org-shared).';
COMMENT ON COLUMN models.scope IS 'built-in (global registry) or instance (owned/org-shared).';
