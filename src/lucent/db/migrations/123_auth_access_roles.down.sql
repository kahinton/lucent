DO $$
DECLARE
    resource RECORD;
BEGIN
    FOR resource IN SELECT * FROM (VALUES
        ('agent_definitions'), ('hook_definitions'), ('memories'),
        ('mcp_server_configs'), ('models'), ('projects'), ('sandboxes'),
        ('sandbox_templates'), ('schedules'), ('secrets'),
        ('skill_definitions')
    ) AS targets(table_name)
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS auth_owner_clearance ON public.%I',
            resource.table_name
        );
        EXECUTE format(
            'DROP TRIGGER IF EXISTS auth_owner_clearance_update ON public.%I',
            resource.table_name
        );
    END LOOP;
END
$$;

DROP FUNCTION IF EXISTS transfer_auth_owner_for_row();
DROP FUNCTION IF EXISTS grant_auth_owner_for_row();

ALTER TABLE auth_clearances
    ADD COLUMN clearance VARCHAR(64)
    CHECK (length(trim(clearance)) > 0);

UPDATE auth_clearances
SET clearance = role;

ALTER TABLE auth_clearances
    DROP CONSTRAINT IF EXISTS auth_clearances_auth_id_principal_type_principal_id_role_key;
ALTER TABLE auth_clearances
    ALTER COLUMN clearance SET NOT NULL;
ALTER TABLE auth_clearances
    DROP COLUMN role;
ALTER TABLE auth_clearances
    DROP COLUMN granted_by;
ALTER TABLE auth_clearances
    ADD CONSTRAINT auth_clearances_auth_id_principal_type_principal_id_clearance_key
    UNIQUE (auth_id, principal_type, principal_id, clearance);
