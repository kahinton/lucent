-- Migration 119: repair owner/daemon memory visibility under RLS.
--
-- The memories policy in migration 116 keyed the privileged-reader branch on
-- ``app.role``. Repository calls carry an explicit user and organization, but
-- not an explicit role; authenticated users therefore defaulted to member.
-- The Python access condition in db/memory.py correctly recognizes the
-- requester's admin/owner/daemon role, while the RLS twin did not, so owners
-- could not read daemon-authored memories even though the application layer
-- admitted them.
--
-- This migration restores the policy contract without changing the memory
-- scope rules above RLS: a scoped 'user' key is still restricted by Python,
-- and RLS does not expand its visibility. The requester identity is checked
-- against the users table (migration 116's global-exempt list), so a member
-- cannot use this branch. Rollback restores the migration-116 policy.

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
         OR EXISTS (
             SELECT 1 FROM public.users memory_requester
             WHERE memory_requester.id = NULLIF(current_setting('app.user_id', true), '')::uuid
               AND memory_requester.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
               AND memory_requester.role IN ('admin', 'owner', 'daemon')
         )
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
         OR EXISTS (
             SELECT 1 FROM public.users memory_requester
             WHERE memory_requester.id = NULLIF(current_setting('app.user_id', true), '')::uuid
               AND memory_requester.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
               AND memory_requester.role IN ('admin', 'owner', 'daemon')
         )
     ))
    OR
    (organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
     AND EXISTS (
         SELECT 1 FROM public.users memory_requester
         WHERE memory_requester.id = NULLIF(current_setting('app.user_id', true), '')::uuid
           AND memory_requester.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
           AND memory_requester.role IN ('admin', 'owner', 'daemon')
     ))
);
