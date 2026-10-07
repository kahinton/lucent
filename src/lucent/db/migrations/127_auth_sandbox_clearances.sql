-- Migration 127: Clearances for the sandbox family (templates + instances).
--
-- Sandbox-template *use* (launching, dispatch, task/schedule attachment)
-- moves to clearance-driven, default-deny reads, matching the definition
-- family (migration 126). Management (create/edit/delete/approve/reject)
-- stays on the org-scoped admin path.
--
-- Sandbox *instances* move to "only yours": members see and control only
-- sandboxes created for them, admins/owners manage all. Until now
-- `sandboxes.created_by` was never persisted — every row was NULL, so the
-- migration-123 owner trigger on `created_by` had nothing to file — and the
-- route checks were org-wide. We backfill owners from the task/request
-- linkage and persist the acting user at creation going forward.
--
-- Built-in templates behave like built-in definitions: an org-wide 'read'
-- clearance at birth, removable by admins via /api/access (the startup
-- template sync only UPDATEs existing rows, so a removed grant stays
-- removed). Admins/owners always see built-ins via management paths.

-- ---------------------------------------------------------------------------
-- 1. Built-in templates = "everyone in this org can use this".
-- Reuses migration 126's generic built-in grant function.
-- ---------------------------------------------------------------------------

CREATE TRIGGER auth_builtin_org_read
AFTER INSERT ON public.sandbox_templates
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_org_read_for_builtin();

-- ---------------------------------------------------------------------------
-- 2. Group-owned templates: 124-style group read clearances (126 skipped
-- sandbox_templates).
-- ---------------------------------------------------------------------------

CREATE TRIGGER auth_group_clearance
AFTER INSERT ON public.sandbox_templates
FOR EACH ROW EXECUTE FUNCTION public.grant_auth_group_clearance_for_row();

-- Backfill existing group-owned templates (group read where no user owner).
INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id)
SELECT t.auth_id, 'read', 'group', t.owner_group_id
FROM public.sandbox_templates t
WHERE t.owner_group_id IS NOT NULL AND t.owner_user_id IS NULL AND t.auth_id IS NOT NULL
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. Built-in backfill: existing built-in templates get their org-wide read.
-- ---------------------------------------------------------------------------

INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id)
SELECT t.auth_id, 'read', 'org', t.organization_id
FROM public.sandbox_templates t
WHERE t.scope = 'built-in' AND t.organization_id IS NOT NULL AND t.auth_id IS NOT NULL
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. Orphaned instance templates: attach them to the org's owner user.
-- Triggers were created above, so the 123 transfer trigger files the owner
-- clearance as the UPDATE lands.
-- ---------------------------------------------------------------------------

UPDATE sandbox_templates t
SET owner_user_id = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id IS NULL
  AND t.organization_id = org.organization_id;

-- ---------------------------------------------------------------------------
-- 5. Sandbox instances: backfill `created_by` — the migration-123 owner
-- trigger on created_by then files each row's owner clearance. Owner
-- resolution mirrors the daemon's task-identity chain: the task's requesting
-- user, else the parent request's creator, else the org's owner user.
-- ---------------------------------------------------------------------------

UPDATE sandboxes s
SET created_by = task.requesting_user_id
FROM tasks task
WHERE s.task_id = task.id
  AND s.created_by IS NULL
  AND s.organization_id IS NOT NULL
  AND task.requesting_user_id IS NOT NULL;

UPDATE sandboxes s
SET created_by = req.created_by
FROM requests req
WHERE s.request_id = req.id
  AND s.created_by IS NULL
  AND s.organization_id IS NOT NULL
  AND req.created_by IS NOT NULL;

UPDATE sandboxes s
SET created_by = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE s.created_by IS NULL
  AND s.organization_id = org.organization_id;