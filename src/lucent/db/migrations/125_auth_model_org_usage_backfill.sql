-- Migration 125: Org-level usage clearances for pre-existing models.
--
-- Models migrated to clearance-driven, default-deny *use* (chat picker, chat
-- turn validation, daemon, request/schedule dropdowns). Before that
-- migration, an org model was implicitly usable by the whole org — no grant
-- step existed. Preserve that state of the world for the rows created before
-- the switchover: give the org principal a 'read' clearance on every enabled
-- org model. New/updated models start closed and must be granted explicitly
-- via the /api/access endpoints; the is_enabled toggle remains the separate
-- "approved for use" gate.
--
-- Only org rows participate in usage: global (organization_id IS NULL)
-- registry rows are managed system-wide and are not backfilled here. Disabled
-- rows are also skipped — they were not usable before the migration, and an
-- admin grants usage when they enable one.

INSERT INTO auth_clearances
    (auth_id, role, principal_type, principal_id)
SELECT models.auth_id, 'read', 'org', models.organization_id
FROM public.models
WHERE models.organization_id IS NOT NULL
  AND models.is_enabled = true
  AND models.auth_id IS NOT NULL
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;