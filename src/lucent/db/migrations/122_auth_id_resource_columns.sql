-- Migration 122: Assign stable auth IDs to resource rows.

CREATE OR REPLACE FUNCTION public.assign_auth_id()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.auth_id IS NULL THEN
        INSERT INTO auth_ids (id, created_at)
        VALUES (gen_random_uuid(), NOW())
        RETURNING id INTO NEW.auth_id;
    ELSE
        INSERT INTO auth_ids (id, created_at)
        VALUES (NEW.auth_id, NOW())
        ON CONFLICT (id) DO NOTHING;
    END IF;

    RETURN NEW;
END;
$$;

-- Delete the resource row's auth ID along with the row. auth_clearances rows
-- follow via auth_ids' ON DELETE CASCADE, so the shared registry never
-- accumulates orphans: every auth_ids row exists because exactly one resource
-- row exists. SECURITY DEFINER keeps cleanup working for callers whose role
-- cannot write auth_ids directly; the resource tables' ON DELETE RESTRICT FK
-- guarantees the row is still uniquely referenced only by the (already
-- deleted) row that fired this trigger.
CREATE OR REPLACE FUNCTION public.purge_auth_id_for_row()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF OLD.auth_id IS NOT NULL THEN
        DELETE FROM auth_ids WHERE id = OLD.auth_id;
    END IF;

    RETURN OLD;
END;
$$;

CREATE TEMP TABLE auth_id_targets (
    table_name TEXT,
    key_column TEXT
) ON COMMIT DROP;

INSERT INTO auth_id_targets (table_name, key_column) VALUES
    ('agent_definitions', 'id'),
    ('api_keys', 'id'),
    ('daemon_instances', 'instance_id'),
    ('enterprise_credentials', 'id'),
    ('hook_definitions', 'id'),
    ('integrations', 'id'),
    ('llm_messages', 'id'),
    ('llm_sessions', 'id'),
    ('managed_tool_definitions', 'id'),
    ('managed_tool_runs', 'id'),
    ('memories', 'id'),
    ('mcp_server_configs', 'id'),
    ('models', 'id'),
    ('projects', 'id'),
    ('requests', 'id'),
    ('reviews', 'id'),
    ('runtime_settings', 'id'),
    ('sandbox_templates', 'id'),
    ('sandboxes', 'id'),
    ('schedule_runs', 'id'),
    ('schedules', 'id'),
    ('secrets', 'id'),
    ('skill_definitions', 'id'),
    ('task_events', 'id'),
    ('task_outputs', 'id'),
    ('tasks', 'id'),
    ('user_file_revisions', 'id'),
    ('user_files', 'id'),
    ('user_interaction_messages', 'id'),
    ('user_interaction_references', 'id'),
    ('user_interactions', 'id'),
    ('user_links', 'id');

CREATE TEMP TABLE auth_id_backfill (
    resource_table TEXT,
    resource_id TEXT,
    auth_id UUID
) ON COMMIT DROP;

UPDATE memories m
SET user_id = COALESCE(m.user_id, owner.user_id),
    organization_id = COALESCE(m.organization_id, owner.organization_id)
FROM (
    SELECT
        username,
        (array_agg(user_id ORDER BY user_id))[1]::text::uuid AS user_id,
        (array_agg(organization_id ORDER BY organization_id))[1]::text::uuid AS organization_id
    FROM memories
    WHERE user_id IS NOT NULL AND organization_id IS NOT NULL
    GROUP BY username
) owner
WHERE m.username = owner.username
  AND m.user_id IS NULL
  AND m.organization_id IS NULL;

DO $$
DECLARE
    target RECORD;
BEGIN
    FOR target IN SELECT table_name, key_column FROM auth_id_targets LOOP
        EXECUTE format(
            'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS auth_id UUID',
            target.table_name
        );
        EXECUTE format(
            'INSERT INTO auth_id_backfill (resource_table, resource_id, auth_id)
             SELECT %L, %I::text, gen_random_uuid()
             FROM public.%I
             WHERE auth_id IS NULL',
            target.table_name,
            target.key_column,
            target.table_name
        );
    END LOOP;
END $$;

INSERT INTO auth_ids (id)
SELECT auth_id
FROM auth_id_backfill
ON CONFLICT (id) DO NOTHING;

DO $$
DECLARE
    target RECORD;
BEGIN
    FOR target IN SELECT table_name, key_column FROM auth_id_targets LOOP
        EXECUTE format(
            'UPDATE public.%I t
             SET auth_id = b.auth_id
             FROM auth_id_backfill b
             WHERE b.resource_table = %L
               AND t.%I::text = b.resource_id',
            target.table_name,
            target.table_name,
            target.key_column
        );
    END LOOP;
END $$;

DO $$
DECLARE
    target RECORD;
BEGIN
    FOR target IN SELECT table_name, key_column FROM auth_id_targets LOOP
        EXECUTE format(
            'ALTER TABLE public.%I ALTER COLUMN auth_id SET NOT NULL',
            target.table_name
        );

        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS uniq_%s_auth_id ON public.%I (auth_id)',
            target.table_name,
            target.table_name
        );

        EXECUTE format(
            'ALTER TABLE public.%I
             ADD CONSTRAINT fk_%s_auth_id
             FOREIGN KEY (auth_id) REFERENCES auth_ids(id) ON DELETE RESTRICT',
            target.table_name,
            target.table_name
        );

        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_assign_auth_id ON public.%I',
            target.table_name,
            target.table_name
        );
        EXECUTE format(
            'CREATE TRIGGER trg_%s_assign_auth_id
             BEFORE INSERT ON public.%I
             FOR EACH ROW EXECUTE FUNCTION public.assign_auth_id()',
            target.table_name,
            target.table_name
        );

        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%s_purge_auth_id ON public.%I',
            target.table_name,
            target.table_name
        );
        EXECUTE format(
            'CREATE TRIGGER trg_%s_purge_auth_id
             AFTER DELETE ON public.%I
             FOR EACH ROW EXECUTE FUNCTION public.purge_auth_id_for_row()',
            target.table_name,
            target.table_name
        );
    END LOOP;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON auth_ids TO lucent_daemon';
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.assign_auth_id() TO lucent_daemon';
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.purge_auth_id_for_row() TO lucent_daemon';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON public.agent_definitions, public.api_keys,
            public.daemon_instances, public.enterprise_credentials, public.hook_definitions,
            public.integrations, public.llm_messages, public.llm_sessions,
            public.managed_tool_definitions, public.managed_tool_runs, public.memories,
            public.mcp_server_configs, public.models, public.projects, public.requests,
            public.reviews, public.runtime_settings, public.sandbox_templates, public.sandboxes,
            public.schedule_runs, public.schedules, public.secrets, public.skill_definitions,
            public.task_events, public.task_outputs, public.tasks, public.user_file_revisions,
            public.user_files, public.user_interaction_messages,
            public.user_interaction_references, public.user_interactions, public.user_links
            TO lucent_daemon';
    END IF;
END $$;
