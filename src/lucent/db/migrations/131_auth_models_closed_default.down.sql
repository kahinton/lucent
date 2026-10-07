-- Undo 131: re-run 125's org backfill so enabled org models are org-usable
-- again. Person/group ownership erased by 131 cannot be restored (the values
-- were deleted); re-grant access explicitly instead.

INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id, granted_by)
SELECT m.auth_id, 'read', 'org', m.organization_id, m.organization_id
FROM public.models m
WHERE m.organization_id IS NOT NULL
  AND m.is_enabled IS TRUE
ON CONFLICT (auth_id, principal_type, principal_id, role) DO NOTHING;