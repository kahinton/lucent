-- Migration 093: Apply standard user/group ownership controls to models.
-- Existing rows remain organization-visible because both owner columns are NULL.

ALTER TABLE models
    ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_group_id UUID REFERENCES groups(id) ON DELETE SET NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'models_single_owner_check'
          AND conrelid = 'models'::regclass
    ) THEN
        ALTER TABLE models ADD CONSTRAINT models_single_owner_check
            CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_models_owner_user
    ON models(organization_id, owner_user_id) WHERE owner_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_models_owner_group
    ON models(organization_id, owner_group_id) WHERE owner_group_id IS NOT NULL;

COMMENT ON COLUMN models.owner_user_id IS
    'User allowed to access this model. NULL when group-owned or organization-visible.';
COMMENT ON COLUMN models.owner_group_id IS
    'Group allowed to access this model. NULL when user-owned or organization-visible.';