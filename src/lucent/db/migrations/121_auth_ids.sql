-- Migration 121: Auth IDs and clearances
--
-- auth_ids is the shared GUID pivot. Other tables (for example, memories and
-- future auth principals) can reference auth_ids.id directly.
--
-- auth_clearances maps a clearance to one user, group, or organization.

CREATE TABLE IF NOT EXISTS auth_ids (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth_clearances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_id UUID NOT NULL REFERENCES auth_ids(id) ON DELETE CASCADE,
    clearance VARCHAR(64) NOT NULL CHECK (length(trim(clearance)) > 0),
    principal_type VARCHAR(10) NOT NULL CHECK (principal_type IN ('user', 'group', 'org')),
    principal_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (auth_id, principal_type, principal_id, clearance)
);

CREATE INDEX IF NOT EXISTS idx_auth_clearances_principal
    ON auth_clearances(principal_type, principal_id);

CREATE INDEX IF NOT EXISTS idx_auth_clearances_auth_id
    ON auth_clearances(auth_id);

COMMENT ON TABLE auth_ids IS
    'Stable GUID registry used as the shared foreign key for auth-related records.';
COMMENT ON TABLE auth_clearances IS
    'Auth clearance mapped to a user, group, or organization principal.';
