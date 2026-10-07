-- Undo migration 127. Order matters: reset the backfilled owners *before*
-- dropping the triggers so the related clearances are removed automatically,
-- then drop the sandbox triggers and clearances.

-- Reset instance-template orphan owners (the 123 transfer trigger cleans
-- their owner clearances).
UPDATE sandbox_templates t
SET owner_user_id = NULL
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NOT NULL
  AND t.owner_group_id IS NULL
  AND t.owner_user_id IN (
      SELECT u.id FROM users u WHERE u.role = 'owner' AND u.organization_id = t.organization_id
  );

-- Reset sandbox created_by backfills (rows that had created_by set only by
-- this migration's UPDATEs): NULL created_by rows cannot carry user clearances
-- via that column, but the UPDATE-transfer trigger removes the clearance when
-- the column is emptied.
UPDATE sandboxes s
SET created_by = NULL
WHERE s.created_by IN (
    -- Owned by the org owner and not linked to a task/request owner:
    SELECT u.id FROM users u WHERE u.role = 'owner' AND u.organization_id = s.organization_id
)
  AND (s.task_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM tasks task WHERE task.id = s.task_id AND task.requesting_user_id = s.created_by
  ))
  AND (s.request_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM requests req WHERE req.id = s.request_id AND req.created_by = s.created_by
  ));

DROP TRIGGER IF EXISTS auth_builtin_org_read ON public.sandbox_templates;
DROP TRIGGER IF EXISTS auth_group_clearance ON public.sandbox_templates;

-- Clear the granted rows this migration added.
DELETE FROM auth_clearances
WHERE principal_type = 'org'
  AND role = 'read'
  AND auth_id IN (SELECT auth_id FROM public.sandbox_templates);

DELETE FROM auth_clearances c
USING public.sandbox_templates t
WHERE c.auth_id = t.auth_id
  AND c.principal_type = 'group'
  AND c.role = 'read'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id = c.principal_id;

-- Note: owner clearances on sandboxes/sandbox_templates filed by the 123
-- triggers via these UPDATEs are cleared by the NOT EXISTS resets above;
-- rows whose original owner was correct all along keep them.