-- Migration 066: Admin audit log
--
-- Tracks administrative and security-sensitive actions that are NOT tied to a
-- specific memory (and therefore don't fit the existing memory_audit_log).
--
-- Examples: user create/update/delete, role change, password change/reset,
-- API key create/revoke, organization rename, group create/delete,
-- impersonation start/stop, settings updates.
--
-- Design notes:
--   * `entity_type` + `entity_id` form a polymorphic reference so we can audit
--     any object without per-object foreign keys.
--   * `action` is free-form TEXT (not a CHECK constraint) so new admin actions
--     can be added without a migration. Use the constants in
--     src/lucent/db/admin_audit.py.
--   * `actor_user_id` is nullable to allow system-initiated actions.
--   * `impersonator_user_id` records the original admin when the action was
--     performed during an impersonation session. This is critical for
--     post-incident review.
--   * `context` is JSONB for flexible metadata (ip, user_agent, request_id, ...).

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- Who performed the action (NULL = system / unauthenticated)
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    -- The original admin if this action was taken while impersonating
    impersonator_user_id UUID REFERENCES users(id) ON DELETE SET NULL,

    -- Polymorphic target
    entity_type TEXT NOT NULL,           -- 'user' | 'organization' | 'api_key' | 'group' | 'session' | 'settings' | 'connection' | 'secret' | 'model'
    entity_id   UUID,                    -- target id when applicable
    entity_label TEXT,                   -- human-readable label captured at action time

    action TEXT NOT NULL,                -- e.g. 'user.create', 'user.role_change', 'org.rename'

    -- Optional structured deltas
    changed_fields TEXT[],
    old_values JSONB,
    new_values JSONB,

    -- Free-form metadata: ip, user_agent, request_id, csrf_session, etc.
    context JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Optional human note (admin-supplied reason)
    notes TEXT,

    -- Outcome — most actions are 'success'; failed/denied attempts can also
    -- be logged for security visibility (e.g. failed role change).
    outcome TEXT NOT NULL DEFAULT 'success' CHECK (outcome IN ('success', 'denied', 'failed')),

    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Org-scoped browsing (most common query)
CREATE INDEX IF NOT EXISTS idx_admin_audit_org_created
    ON admin_audit_log (organization_id, created_at DESC);

-- Find actions a user took
CREATE INDEX IF NOT EXISTS idx_admin_audit_actor
    ON admin_audit_log (actor_user_id, created_at DESC)
    WHERE actor_user_id IS NOT NULL;

-- Find actions taken AGAINST an entity (e.g. all changes to user X)
CREATE INDEX IF NOT EXISTS idx_admin_audit_entity
    ON admin_audit_log (entity_type, entity_id, created_at DESC)
    WHERE entity_id IS NOT NULL;

-- Filter by action type
CREATE INDEX IF NOT EXISTS idx_admin_audit_action
    ON admin_audit_log (organization_id, action, created_at DESC);

COMMENT ON TABLE admin_audit_log IS
    'Audit trail for administrative/security actions (users, org, API keys, groups, sessions). Use AdminAuditRepository.';
