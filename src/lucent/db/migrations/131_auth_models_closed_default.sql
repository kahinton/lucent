-- 131: models close and de-own.
--
-- Kyle's settled design (2026-10-02): "Models belong to the org, period.
-- Ownership is not an access-level thing, and the org does not get access to
-- models by default." Two corrections to migration 125's continuity posture:
--
--   1. The backfill granted ('read','org') on every pre-existing enabled org
--      model — an org-wide default the design does not want. All of them are
--      removed; access now comes only from explicit grants (the web Grant UI
--      or /api/access). This includes admins/owners: list_models_accessible_by
--      is already role-independent, so nothing is usable until granted.
--   2. Person/group ownership rows are stripped. The UPDATE transfer trigger
--      (123) deletes their owner clearances automatically; from now on no
--      model carries an owner clearance and management of grants stays
--      admin-gated (owner is NULL -> _owner_is_manager requires an admin role).
--
-- The owner_* columns and their triggers stay (definitions/schedules keep the
-- concept; models simply never have owners again).
--
-- Reversal (.down) re-runs 125's org backfill for enabled org models but
-- cannot restore erased owner columns — accepted, same trade-off as 127/129.

-- 1. Close org-wide model grants (the 125 backfill and any other org grant).
DELETE FROM auth_clearances acl
USING models m
WHERE acl.auth_id = m.auth_id
  AND acl.principal_type = 'org'
  AND m.organization_id IS NOT NULL;

-- 2. Strip ownership. The transfer trigger drops ('owner','user') rows.
UPDATE models
SET owner_user_id = NULL,
    owner_group_id = NULL
WHERE organization_id IS NOT NULL
  AND (owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL);

-- 3. Safety: no stray owner clearances may survive on models.
DELETE FROM auth_clearances acl
USING models m
WHERE acl.auth_id = m.auth_id
  AND acl.role = 'owner'
  AND m.organization_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM models m2
      WHERE m2.auth_id = acl.auth_id
        AND m2.owner_user_id = acl.principal_id
  );