-- Migration 128: Owner clearances for the schedules family.
--
-- Schedule *use* (listing a user's own workflows, opening details, triggering)
-- moves to clearance-driven, default-deny reads on `schedules` — the
-- definition-family pattern. Migration 123 already gave this table the
-- owner-clearance INSERT/UPDATE triggers on `created_by` and backfilled
-- existing rows, and the "schedule" resource spec already exists in
-- AUTH_ACCESS_RESOURCES (so group/explicit grants work via /api/access).
-- Only orphan handling remains: schedules whose `created_by` is NULL have no
-- owner clearance and would otherwise be invisible/unusable.
--
-- `schedule_runs` stays parent-gated (no org/owner columns; runs are only
-- ever read through their schedule) — deliberately not clearance-backed.

-- ---------------------------------------------------------------------------
-- 1. Orphaned non-system schedules: attach them to the org's owner user.
-- Triggers exist since 123, so the transfer trigger files the owner
-- clearance as the UPDATE lands.
-- ---------------------------------------------------------------------------
UPDATE schedules s
SET created_by = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE s.created_by IS NULL
  AND s.is_system IS NOT TRUE
  AND s.organization_id IS NOT NULL
  AND s.organization_id = org.organization_id;

-- ---------------------------------------------------------------------------
-- 2. Orphaned system schedules: attach them to the org's daemon-service user
-- (the principal that actually owns/runs system schedules; same identity
-- ensure_daemon_service_user provisions: external_id = 'daemon-service:<org>').
-- ---------------------------------------------------------------------------
UPDATE schedules s
SET created_by = svc.id
FROM users svc
WHERE svc.provider = 'local'
  AND svc.external_id = 'daemon-service:' || s.organization_id::text
  AND s.created_by IS NULL
  AND s.is_system IS TRUE;

-- ---------------------------------------------------------------------------
-- 3. Safety backfill: schedule rows whose owner clearance is missing despite a
-- set `created_by` (e.g. rows predating a trigger repair). Owner-only roles
-- can't self-heal, so grant the owner user directly.
-- ---------------------------------------------------------------------------
INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id)
SELECT s.auth_id, 'owner', 'user', s.created_by
FROM public.schedules s
WHERE s.created_by IS NOT NULL
  AND s.auth_id IS NOT NULL
  AND s.created_by IN (
      SELECT u.id FROM users u
      WHERE u.role = 'owner' AND u.organization_id = s.organization_id
  )
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

-- If no rows were inserted by section 3, drift is zero (expected).

-- ---------------------------------------------------------------------------
-- 4. Member-schedule visibility: schedules are "only yours" per the
-- definitions/sandbox pattern — no org-wide read grants are stamped. A
-- member sees and controls only schedules they created or that were
-- granted (user/group) explicitly; admins/owners keep the org-wide
-- management view (incl. is_system and daemon-created rows) on the legacy
-- path.
-- ---------------------------------------------------------------------------