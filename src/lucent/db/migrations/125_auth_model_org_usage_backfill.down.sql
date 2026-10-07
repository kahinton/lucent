-- Undo migration 125: remove the org usage clearances backfilled for
-- pre-existing models. Any org clearance added to a model *after* the
-- backfill (via the /api/access grant endpoints) is indistinguishable and is
-- removed as well — re-grant it if needed.

DELETE FROM auth_clearances
WHERE principal_type = 'org'
  AND role = 'read'
  AND auth_id IN (SELECT auth_id FROM public.models);