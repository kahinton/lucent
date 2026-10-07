-- Undo migration 128. The UPDATEs reset the backfilled owners *first*, so
-- migration 123's transfer trigger removes the corresponding owner
-- clearances automatically. Section 128.3's safety grants (rows whose
-- `created_by` was already set) are left in place — they're identical to the
-- clearances migration 123 filed originally and removing them would strand
-- genuinely owned schedules.

-- Reset orphaned non-system schedules that this migration re-assigned to the
-- org's first owner user. Heuristic, same trade-off as migration 127's down:
-- a schedule genuinely created by that first owner user loses its re-assign
-- (its owner column stays set; only rows we touched go back to NULL).
UPDATE schedules s
SET created_by = NULL
WHERE s.created_by IS NOT NULL
  AND s.is_system IS NOT TRUE
  AND s.created_by IN (
      SELECT first_owner FROM (
          SELECT DISTINCT ON (organization_id) organization_id, id AS first_owner
          FROM users
          WHERE role = 'owner' AND organization_id IS NOT NULL
          ORDER BY organization_id, created_at
      ) o
      WHERE o.organization_id = s.organization_id
  );

-- Reset orphaned system schedules this migration attached to the org's
-- daemon-service user.
UPDATE schedules s
SET created_by = NULL
WHERE s.created_by IS NOT NULL
  AND s.is_system IS TRUE
  AND s.created_by IN (
      SELECT u.id FROM users u
      WHERE u.provider = 'local'
        AND u.external_id = 'daemon-service:' || s.organization_id::text
  );