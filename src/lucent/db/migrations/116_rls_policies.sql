-- Migration 116: RLS wave (FORCE RLS + tenant policies) — the structural
-- mechanism from the 9/10 enforcement-mechanism evaluation §6 (originally
-- numbered 111/112; 111-115 were consumed by the projects feature,
-- handoff-privacy hardening, and the orphan-table provisioning, so this
-- wave is 114/115/116).
--
-- SELF-GATING: this migration aborts unless the server pool has been cut
-- over to the lucent_app role. The gate lives in the migration runner
-- (db/pool.py: _ensure_rls_wave_preconditions), which refuses to apply
-- 116 while the runner session is superuser/BYPASSRLS. FORCE RLS cannot
-- protect a superuser session, so pre-cutover application would give only
-- the appearance of enforcement.
--
-- TABLES ARE BOUND EXPLICITLY (no loops): every table below is listed by
-- name, twice (ENABLE + FORCE). The list is the reviewed audit surface.
-- Tables created after this migration must opt in via their own migration —
-- the static SQL-scoping gate (Phase A) is the authoring-time net for those.
--
-- SHAPE CONTRACT (Kyle-approved §5.3 policy classes):
--   (a) flat user+org           member rows private; admin/owner/daemon
--                               org-wide within app.org_id (org is the tenant
--                               boundary; requester-visibility stays a product
--                               rule above the RLS layer).
--   (b) org-only work items     org-wide by design; requester-visibility stays
--                               a product rule in _request_visibility_condition
--                               (db/requests.py).
--   (c) memories                transliteration of
--                               user_memory_access_condition (db/memory.py);
--                               daemon-authored rows readable only by
--                               privileged readers.
--   (d) child tables            EXISTS-to-parent; org + ownership transit.
--
-- FAIL-CLOSED PROPERTY: context unset => app.org_id empty => policy predicate
-- false => zero rows. No policy for a table => no access. The global-exempt
-- list (NOT bound here) is explicit and reviewed: organizations, users,
-- user_sessions, schema_migrations, groups, models, runtime_settings.

-- ===========================================================================
-- 1. Bind every non-exempt table: ENABLE + FORCE row level security.
--    (Ownership was already reassigned to lucent_app by migration 114.)
-- ===========================================================================

ALTER TABLE public.admin_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_audit_log FORCE ROW LEVEL SECURITY;
ALTER TABLE public.agent_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_definitions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.agent_hooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_hooks FORCE ROW LEVEL SECURITY;
ALTER TABLE public.agent_managed_tools ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_managed_tools FORCE ROW LEVEL SECURITY;
ALTER TABLE public.agent_mcp_servers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_mcp_servers FORCE ROW LEVEL SECURITY;
ALTER TABLE public.agent_skills ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_skills FORCE ROW LEVEL SECURITY;
ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.api_keys FORCE ROW LEVEL SECURITY;
ALTER TABLE public.daemon_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daemon_instances FORCE ROW LEVEL SECURITY;
ALTER TABLE public.enterprise_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.enterprise_credentials FORCE ROW LEVEL SECURITY;
ALTER TABLE public.github_repo_access_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.github_repo_access_cache FORCE ROW LEVEL SECURITY;
ALTER TABLE public.hook_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.hook_definitions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.integrations FORCE ROW LEVEL SECURITY;
ALTER TABLE public.llm_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_messages FORCE ROW LEVEL SECURITY;
ALTER TABLE public.llm_session_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_session_events FORCE ROW LEVEL SECURITY;
ALTER TABLE public.llm_session_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_session_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE public.llm_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.llm_token_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_token_usage FORCE ROW LEVEL SECURITY;
ALTER TABLE public.managed_tool_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.managed_tool_definitions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.managed_tool_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.managed_tool_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.mcp_server_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mcp_server_configs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memories FORCE ROW LEVEL SECURITY;
ALTER TABLE public.memory_access_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_access_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE public.memory_access_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_access_log FORCE ROW LEVEL SECURITY;
ALTER TABLE public.memory_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_audit_log FORCE ROW LEVEL SECURITY;
ALTER TABLE public.memory_shadow_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_shadow_scores FORCE ROW LEVEL SECURITY;
ALTER TABLE public.oauth2_state_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.oauth2_state_challenges FORCE ROW LEVEL SECURITY;
ALTER TABLE public.pairing_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pairing_challenges FORCE ROW LEVEL SECURITY;
ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.projects FORCE ROW LEVEL SECURITY;
ALTER TABLE public.request_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.request_memories FORCE ROW LEVEL SECURITY;
ALTER TABLE public.request_views ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.request_views FORCE ROW LEVEL SECURITY;
ALTER TABLE public.requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.requests FORCE ROW LEVEL SECURITY;
ALTER TABLE public.resource_access_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.resource_access_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE public.reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reviews FORCE ROW LEVEL SECURITY;
ALTER TABLE public.sandbox_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sandbox_templates FORCE ROW LEVEL SECURITY;
ALTER TABLE public.sandboxes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sandboxes FORCE ROW LEVEL SECURITY;
ALTER TABLE public.schedule_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.schedule_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.schedules ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.schedules FORCE ROW LEVEL SECURITY;
ALTER TABLE public.secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.secrets FORCE ROW LEVEL SECURITY;
ALTER TABLE public.skill_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.skill_definitions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.task_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_events FORCE ROW LEVEL SECURITY;
ALTER TABLE public.task_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_memories FORCE ROW LEVEL SECURITY;
ALTER TABLE public.task_outputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_outputs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE public.tool_call_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tool_call_audit_log FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_file_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_file_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_file_views ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_file_views FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_files FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_groups FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_interaction_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_interaction_messages FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_interaction_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_interaction_references FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_interaction_views ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_interaction_views FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_interactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_interactions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.user_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_links FORCE ROW LEVEL SECURITY;

-- ===========================================================================
-- 2. Tenant policies.
-- ===========================================================================

-- -------------------------------------------------------------------
-- Shape (a): flat user+org — member rows private; admin/owner/daemon
-- org-wide within app.org_id. 15 tables.
-- -------------------------------------------------------------------

CREATE POLICY p_api_keys_tenant ON public.api_keys USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_llm_sessions_tenant ON public.llm_sessions USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_llm_token_usage_tenant ON public.llm_token_usage USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_managed_tool_runs_tenant ON public.managed_tool_runs USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_projects_tenant ON public.projects USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_request_views_tenant ON public.request_views USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_tool_call_audit_log_tenant ON public.tool_call_audit_log USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_user_file_revisions_tenant ON public.user_file_revisions USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_user_file_views_tenant ON public.user_file_views USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_user_files_tenant ON public.user_files USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_user_interaction_views_tenant ON public.user_interaction_views USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_user_interactions_tenant ON public.user_interactions USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_user_links_tenant ON public.user_links USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_memory_audit_log_tenant ON public.memory_audit_log USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

CREATE POLICY p_memory_access_log_tenant ON public.memory_access_log USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    AND (
        user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
        OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
    )
);

-- -------------------------------------------------------------------
-- Shape (b): org-only work items — org-wide by design.
-- Requester-visibility stays a product rule in _request_visibility_condition
-- (db/requests.py:577-604); encoding it here would make daemon fan-outs
-- brittle and RLS the wrong layer for a product rule. 'system' session role
-- passes without org context for the enumerated system-infra paths.
-- -------------------------------------------------------------------

CREATE POLICY p_requests_tenant ON public.requests USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_tasks_tenant ON public.tasks USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_task_outputs_tenant ON public.task_outputs USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_schedules_tenant ON public.schedules USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_secrets_tenant ON public.secrets USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_reviews_tenant ON public.reviews USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_sandboxes_tenant ON public.sandboxes USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_sandbox_templates_tenant ON public.sandbox_templates USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_integrations_tenant ON public.integrations USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_mcp_server_configs_tenant ON public.mcp_server_configs USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_agent_definitions_tenant ON public.agent_definitions USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_skill_definitions_tenant ON public.skill_definitions USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_hook_definitions_tenant ON public.hook_definitions USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_managed_tool_definitions_tenant ON public.managed_tool_definitions USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_daemon_instances_tenant ON public.daemon_instances USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_enterprise_credentials_tenant ON public.enterprise_credentials USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_oauth2_state_challenges_tenant ON public.oauth2_state_challenges USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_resource_access_grants_tenant ON public.resource_access_grants USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_admin_audit_log_tenant ON public.admin_audit_log USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_memory_access_grants_tenant ON public.memory_access_grants USING (
    organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    OR current_setting('app.role', true) = 'system'
);

-- -------------------------------------------------------------------
-- Shape (d): child + junction tables — EXISTS-to-parent with org + ownership
-- transit (20 tables). 'system' session role passes without org context for
-- the enumerated system-infra paths.
-- -------------------------------------------------------------------

CREATE POLICY p_schedule_runs_tenant ON public.schedule_runs USING (
    EXISTS (
        SELECT 1 FROM public.schedules s
        WHERE s.id = public.schedule_runs.schedule_id
          AND s.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_llm_messages_tenant ON public.llm_messages USING (
    EXISTS (
        SELECT 1 FROM public.llm_sessions s
        WHERE s.id = public.llm_messages.session_id
          AND s.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
          AND (
              s.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
              OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
          )
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_llm_session_events_tenant ON public.llm_session_events USING (
    EXISTS (
        SELECT 1 FROM public.llm_sessions s
        WHERE s.id = public.llm_session_events.session_id
          AND s.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
          AND (
              s.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
              OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
          )
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_llm_session_requests_tenant ON public.llm_session_requests USING (
    EXISTS (
        SELECT 1 FROM public.llm_sessions s
        WHERE s.id = public.llm_session_requests.session_id
          AND s.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
          AND (
              s.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
              OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
          )
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_memory_shadow_scores_tenant ON public.memory_shadow_scores USING (
    EXISTS (
        SELECT 1 FROM public.memories m
        WHERE m.id = public.memory_shadow_scores.memory_id
          AND m.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_request_memories_tenant ON public.request_memories USING (
    EXISTS (
        SELECT 1 FROM public.requests r
        WHERE r.id = public.request_memories.request_id
          AND r.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_task_events_tenant ON public.task_events USING (
    EXISTS (
        SELECT 1 FROM public.tasks t
        WHERE t.id = public.task_events.task_id
          AND t.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_task_memories_tenant ON public.task_memories USING (
    EXISTS (
        SELECT 1 FROM public.tasks t
        WHERE t.id = public.task_memories.task_id
          AND t.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_user_interaction_messages_tenant ON public.user_interaction_messages USING (
    EXISTS (
        SELECT 1 FROM public.user_interactions ui
        WHERE ui.id = public.user_interaction_messages.interaction_id
          AND ui.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
          AND (
              ui.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
              OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
          )
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_user_interaction_references_tenant ON public.user_interaction_references USING (
    EXISTS (
        SELECT 1 FROM public.user_interactions ui
        WHERE ui.id = public.user_interaction_references.interaction_id
          AND ui.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
          AND (
              ui.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
              OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
          )
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_agent_hooks_tenant ON public.agent_hooks USING (
    EXISTS (
        SELECT 1 FROM public.agent_definitions a
        WHERE a.id = public.agent_hooks.agent_id
          AND a.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_agent_managed_tools_tenant ON public.agent_managed_tools USING (
    EXISTS (
        SELECT 1 FROM public.agent_definitions a
        WHERE a.id = public.agent_managed_tools.agent_id
          AND a.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_agent_mcp_servers_tenant ON public.agent_mcp_servers USING (
    EXISTS (
        SELECT 1 FROM public.agent_definitions a
        WHERE a.id = public.agent_mcp_servers.agent_id
          AND a.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_agent_skills_tenant ON public.agent_skills USING (
    EXISTS (
        SELECT 1 FROM public.agent_definitions a
        WHERE a.id = public.agent_skills.agent_id
          AND a.organization_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    )
    OR current_setting('app.role', true) = 'system'
);

CREATE POLICY p_github_repo_access_cache_tenant ON public.github_repo_access_cache USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
);

CREATE POLICY p_pairing_challenges_tenant ON public.pairing_challenges USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
);

CREATE POLICY p_user_groups_tenant ON public.user_groups USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
    OR current_setting('app.role', true) IN ('admin', 'owner', 'daemon', 'system')
);

-- -------------------------------------------------------------------
-- Shape (c): memories — transliteration of user_memory_access_condition
-- (db/memory.py:108-198). The Python condition is the contract; the policy
-- is its engine-side twin. Cross-check tests pin the two together.
--
-- Arms (OR'd):
--   1. daemon/system authoring path (org-keyed, writes from daemon sessions)
--   2. legacy NULL user_id rows: org-privileged visibility only
--   3. plain ownership (non-daemon-authored, or privileged reader)
--   4. granted: org/user/group grants, index-supported (7 indexes verified)
--   5. org-wide diagnostic branch for admin/owner/daemon within their org —
--      matches the Python admin/owner reader class (rbac.py:169).
-- -------------------------------------------------------------------

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