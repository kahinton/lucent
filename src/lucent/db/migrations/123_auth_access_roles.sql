-- Migration 123: Turn clearances into resource ACL roles.

ALTER TABLE auth_clearances
    ADD COLUMN role VARCHAR(8),
    ADD COLUMN granted_by UUID REFERENCES users(id) ON DELETE SET NULL;

UPDATE auth_clearances
SET role = CASE clearance
    WHEN 'owner' THEN 'owner'
    WHEN 'write' THEN 'write'
    ELSE 'read'
END
WHERE role IS NULL;

ALTER TABLE auth_clearances
    ALTER COLUMN role SET NOT NULL;

ALTER TABLE auth_clearances
    DROP CONSTRAINT IF EXISTS auth_clearances_auth_id_principal_type_principal_id_clearance_key;
ALTER TABLE auth_clearances
    DROP CONSTRAINT IF EXISTS auth_clearances_clearance_check;
ALTER TABLE auth_clearances
    DROP COLUMN IF EXISTS clearance;

ALTER TABLE auth_clearances
    ADD CONSTRAINT auth_clearances_role_check
    CHECK (role IN ('read', 'write', 'owner'));

ALTER TABLE auth_clearances
    DROP CONSTRAINT IF EXISTS auth_clearances_auth_id_principal_type_principal_id_role_key;
ALTER TABLE auth_clearances
    ADD CONSTRAINT auth_clearances_auth_id_principal_type_principal_id_role_key
    UNIQUE (auth_id, principal_type, principal_id, role);

CREATE INDEX IF NOT EXISTS idx_auth_clearances_auth_id_role
    ON auth_clearances(auth_id, role);

CREATE OR REPLACE FUNCTION public.grant_auth_owner_for_row()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    owner_id UUID;
BEGIN
    owner_id := (to_jsonb(NEW) ->> TG_ARGV[0])::uuid;
    IF owner_id IS NULL THEN
        RETURN NEW;
    END IF;

    INSERT INTO auth_clearances
        (auth_id, role, principal_type, principal_id, granted_by)
    VALUES
        (NEW.auth_id, 'owner', 'user', owner_id, owner_id)
    ON CONFLICT (auth_id, principal_type, principal_id, role)
    DO NOTHING;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.transfer_auth_owner_for_row()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    owner_column TEXT := TG_ARGV[0];
    old_owner_id UUID := (to_jsonb(OLD) ->> owner_column)::uuid;
    new_owner_id UUID := (to_jsonb(NEW) ->> owner_column)::uuid;
BEGIN
    IF old_owner_id IS NOT DISTINCT FROM new_owner_id THEN
        RETURN NEW;
    END IF;

    IF old_owner_id IS NOT NULL THEN
        DELETE FROM auth_clearances
        WHERE auth_id = NEW.auth_id
          AND role = 'owner'
          AND principal_type = 'user'
          AND principal_id = old_owner_id;
    END IF;

    IF new_owner_id IS NOT NULL THEN
        INSERT INTO auth_clearances
            (auth_id, role, principal_type, principal_id, granted_by)
        VALUES
            (NEW.auth_id, 'owner', 'user', new_owner_id, new_owner_id)
        ON CONFLICT (auth_id, principal_type, principal_id, role)
        DO NOTHING;
    END IF;

    RETURN NEW;
END;
$$;

DO $$
DECLARE
    resource RECORD;
    owner_column TEXT;
BEGIN
    -- Only the genuinely shareable resources get per-row ownership
    -- clearances. High-volume private tables (llm_messages, task_events,
    -- requests, ...) stay on direct ownership columns; giving them ACL rows
    -- would cost an extra INSERT per message/event, and clearances would
    -- never be used there anyway. Owner columns must match
    -- AUTH_ACCESS_RESOURCES in lucent.db.access_control (guarded by
    -- tests/test_auth_access_sync.py): definition tables use owner_user_id —
    -- the legacy sharing semantic, NULL for built-in rows — not the
    -- creator-audit created_by column.
    FOR resource IN SELECT * FROM (VALUES
        ('agent_definitions', 'id', 'owner_user_id'),
        ('hook_definitions', 'id', 'owner_user_id'),
        ('memories', 'id', 'user_id'),
        ('mcp_server_configs', 'id', 'owner_user_id'),
        ('models', 'id', 'owner_user_id'),
        ('projects', 'id', 'user_id'),
        ('sandboxes', 'id', 'created_by'),
        ('sandbox_templates', 'id', 'owner_user_id'),
        ('schedules', 'id', 'created_by'),
        ('secrets', 'id', 'owner_user_id'),
        ('skill_definitions', 'id', 'owner_user_id')
    ) AS targets(table_name, id_column, owner_column)
    LOOP
        SELECT columns.column_name INTO owner_column
        FROM information_schema.columns AS columns
        WHERE columns.table_schema = 'public'
          AND columns.table_name = resource.table_name
          AND columns.column_name = resource.owner_column
        LIMIT 1;
        IF owner_column IS NULL THEN
            -- Unreachable if tests/test_auth_access_sync.py passes; kept as a
            -- runtime guard so a schema drift skips the resource loudly
            -- rather than creating triggers on a missing column.
            RAISE NOTICE 'auth owner trigger skipped: %.% does not exist',
                resource.table_name, resource.owner_column;
            CONTINUE;
        END IF;

        EXECUTE format(
            'CREATE TRIGGER auth_owner_clearance
             AFTER INSERT ON public.%I
             FOR EACH ROW EXECUTE FUNCTION public.grant_auth_owner_for_row(%L)',
            resource.table_name,
            resource.owner_column,
            resource.owner_column
        );

        EXECUTE format(
            'CREATE TRIGGER auth_owner_clearance_update
             AFTER UPDATE ON public.%I
             FOR EACH ROW WHEN (OLD.%I IS DISTINCT FROM NEW.%I)
             EXECUTE FUNCTION public.transfer_auth_owner_for_row(%L)',
            resource.table_name,
            resource.owner_column,
            resource.owner_column,
            resource.owner_column
        );

        EXECUTE format(
            $sql$
                INSERT INTO auth_clearances
                    (auth_id, role, principal_type, principal_id, granted_by)
                SELECT resource.auth_id, 'owner', 'user', resource.%I, resource.%I
                FROM public.%I AS resource
                WHERE resource.%I IS NOT NULL AND resource.auth_id IS NOT NULL
                ON CONFLICT (auth_id, principal_type, principal_id, role)
                DO NOTHING
            $sql$,
            resource.owner_column,
            resource.owner_column,
            resource.table_name,
            resource.owner_column
        );
    END LOOP;
END
$$;
