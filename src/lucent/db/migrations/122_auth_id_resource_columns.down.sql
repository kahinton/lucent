-- Rollback migration 122.

DROP FUNCTION IF EXISTS public.purge_auth_id_for_row();
DROP FUNCTION IF EXISTS public.assign_auth_id();

DO $$
DECLARE
    target RECORD;
BEGIN
    FOR target IN
        SELECT unnest(ARRAY[
            'agent_definitions', 'api_keys', 'daemon_instances',
            'enterprise_credentials', 'hook_definitions', 'integrations',
            'llm_messages', 'llm_sessions', 'managed_tool_definitions',
            'managed_tool_runs', 'memories', 'mcp_server_configs', 'models',
            'projects', 'requests', 'reviews', 'runtime_settings',
            'sandbox_templates', 'sandboxes', 'schedule_runs', 'schedules',
            'secrets', 'skill_definitions', 'task_events', 'task_outputs',
            'tasks', 'user_file_revisions', 'user_files',
            'user_interaction_messages', 'user_interaction_references',
            'user_interactions', 'user_links'
        ]) AS table_name
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_assign_auth_id ON public.%I',
            target.table_name,
            target.table_name
        );
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_purge_auth_id ON public.%I',
            target.table_name,
            target.table_name
        );
        EXECUTE format(
            'ALTER TABLE public.%I DROP COLUMN IF EXISTS auth_id',
            target.table_name
        );
    END LOOP;
END $$;
