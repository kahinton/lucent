-- Rollback 119: restore the original migration-116 memory policy. RLS stays
-- active; daemon-authored rows return to the app.role-dependent visibility
-- behavior that this repair fixes.

DROP POLICY IF EXISTS p_memories_tenant ON public.memories;

CREATE POLICY p_memories_tenant ON public.memories USING (
    (current_setting('app.role', true) IN ('daemon', 'system')
     AND organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid)
    OR
    (user_id IS NULL
     AND current_setting('app.role', true) IN ('admin', 'owner', 'system')
     AND organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid)
    OR
    (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
     AND (
         NOT EXISTS (
             SELECT 1 FROM public.users memory_owner
             WHERE memory_owner.id = public.memories.user_id
               AND (
                   memory_owner.role = 'daemon'
                   OR memory_owner.external_id = 'daemon-service'
                   OR memory_owner.external_id LIKE 'daemon-service:%'
               )
         )
         OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon')
     ))
    OR
    (organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
     AND EXISTS (
         SELECT 1 FROM public.memory_access_grants memory_grant
         WHERE memory_grant.memory_id = public.memories.id
           AND memory_grant.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
           AND (
               memory_grant.grantee_type = 'organization'
               OR (memory_grant.grantee_type = 'user'
                   AND memory_grant.grantee_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
               OR (memory_grant.grantee_type = 'group'
                   AND memory_grant.grantee_group_id IN (
                       SELECT memory_group.group_id
                       FROM public.user_groups memory_group
                       WHERE memory_group.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                   ))
           )
     )
     AND (
         NOT EXISTS (
             SELECT 1 FROM public.users memory_owner
             WHERE memory_owner.id = public.memories.user_id
               AND (
                   memory_owner.role = 'daemon'
                   OR memory_owner.external_id = 'daemon-service'
                   OR memory_owner.external_id LIKE 'daemon-service:%'
               )
         )
         OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon')
     ))
    OR
    (organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
     AND current_setting('app.role', true) IN ('admin', 'owner', 'daemon'))
);
