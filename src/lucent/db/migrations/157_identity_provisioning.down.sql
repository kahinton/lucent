-- Down migration for 157: remove identity-provisioning state.
--
-- Order matters: every table here has an FK to organizations, and
-- `users.provider` is narrowed last so no row can violate the older CHECK
-- while the newer tables still reference those rows.

DROP TABLE IF EXISTS scim_tokens;
DROP TABLE IF EXISTS saml_assertion_replays;
DROP TABLE IF EXISTS sso_login_states;
DROP TABLE IF EXISTS sso_connections;
DROP TABLE IF EXISTS identity_invitations;

DROP INDEX IF EXISTS idx_users_org_scim_external_id;
DROP INDEX IF EXISTS idx_users_org_email_unique;

ALTER TABLE users DROP COLUMN IF EXISTS provisioned_by;
ALTER TABLE users DROP COLUMN IF EXISTS scim_external_id;
ALTER TABLE users DROP COLUMN IF EXISTS email_verified;

-- Providers written by the OIDC flow cannot be represented by the pre-157
-- CHECK. Re-label them `local` rather than deleting the accounts: the human is
-- still a real member of the tenant, they simply lose the federated label.
UPDATE users SET provider = 'local' WHERE provider = 'oidc';

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_provider_check;
ALTER TABLE users
    ADD CONSTRAINT users_provider_check
    CHECK (provider IN ('google', 'github', 'saml', 'local', 'basic'));

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_provisioned_by_check;
