DROP TRIGGER IF EXISTS auth_group_clearance ON public.secrets;
DROP FUNCTION IF EXISTS grant_auth_group_clearance_for_row();

DELETE FROM auth_clearances
WHERE role = 'read'
  AND principal_type = 'group'
  AND auth_id IN (SELECT auth_id FROM secrets WHERE auth_id IS NOT NULL);