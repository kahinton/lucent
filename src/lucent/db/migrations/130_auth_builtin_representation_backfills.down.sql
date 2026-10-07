-- Down for 130: remove the owner clearances this migration filed on
-- managed_tool_definitions rows (grants / group grants that already existed
-- are untouched; we only delete the rows our INSERT could have created).
DELETE FROM auth_clearances c
USING public.managed_tool_definitions t
WHERE c.auth_id = t.auth_id
  AND c.role = 'owner'
  AND c.principal_type = 'user'
  AND c.principal_id = t.owner_user_id;
