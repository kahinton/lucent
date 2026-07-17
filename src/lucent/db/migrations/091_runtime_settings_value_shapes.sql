-- Migration 091: Enforce runtime setting JSON value shapes
--
-- Previous state: value_type named the intended application type, but the
-- JSONB value itself could contain a different shape if a caller bypassed the
-- Settings service. Existing application writes already use native JSON
-- booleans/numbers/strings, so this is an additive integrity constraint.
--
-- Rollback:
--   ALTER TABLE runtime_settings
--     DROP CONSTRAINT IF EXISTS runtime_settings_value_shape_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'runtime_settings'::regclass
          AND conname = 'runtime_settings_value_shape_check'
    ) THEN
        ALTER TABLE runtime_settings
            ADD CONSTRAINT runtime_settings_value_shape_check CHECK (
                (value_type = 'boolean' AND jsonb_typeof(value) = 'boolean')
                OR (
                    value_type = 'integer'
                    AND jsonb_typeof(value) = 'number'
                    AND value #>> '{}' ~ '^-?[0-9]+$'
                )
                OR (value_type = 'float' AND jsonb_typeof(value) = 'number')
                OR (value_type = 'string' AND jsonb_typeof(value) = 'string')
                OR value_type = 'json'
            );
    END IF;
END $$;

COMMENT ON CONSTRAINT runtime_settings_value_shape_check ON runtime_settings IS
    'Ensures JSONB values match the canonical runtime setting value_type.';
