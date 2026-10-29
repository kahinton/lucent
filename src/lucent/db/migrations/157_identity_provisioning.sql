-- Migration 157: Identity provisioning — invitations, SSO connections, SCIM.
--
-- Lucent previously supported exactly one way to get a user into an
-- organization: an administrator hand-created the account and handed over a
-- generated temporary password out of band. That is an adequate operator flow
-- but a poor product flow, and it has no answer for self-serve signup,
-- shareable invite links, or enterprise identity federation.
--
-- This migration adds the durable state for four additions, all of which share
-- one property: a party outside the tenant asserts an identity, so every row
-- here is a credential or a trust anchor and is treated accordingly.
--
--   1. identity_invitations  — single-use, expiring invite links.
--   2. sso_connections       — per-tenant OIDC / SAML IdP configuration.
--   3. sso_login_states      — single-use, expiring in-flight login state
--                              (OAuth state + PKCE verifier + OIDC nonce, or
--                              SAML RelayState + InResponseTo).
--   4. saml_assertion_replays — assertion-ID replay cache. SAML has no state
--                              cookie on the assertion itself, so the only way
--                              to reject a replayed assertion is to remember
--                              the IDs already spent.
--   5. scim_tokens           — org-scoped bearer credentials for an external
--                              identity provider to call the SCIM 2.0 API.
--
-- It also tightens two things on `users` that identity federation depends on:
--
--   * `email_verified` records whether the address was proven. Auto-linking an
--     incoming SSO identity to a pre-existing local account by email is only
--     safe when the address in question was actually asserted by somebody; the
--     column makes that distinction explicit instead of implied.
--   * A partial unique index on (organization_id, lower(email)) for humans.
--     Without it, two members of one tenant can share an email and an SSO
--     login by email becomes ambiguous — which is an account-takeover shape,
--     not a nuisance. Daemon service identities are excluded because several
--     share `daemon@lucent.local` by construction.
--
-- `users.scim_external_id` records the IdP's own identifier for a SCIM-managed
-- user. It is deliberately separate from `external_id`/`provider`, which
-- describe how the human proves who they are at login. A SCIM-provisioned user
-- may still log in with a password or through SSO, so those are orthogonal
-- facts and must not share a column.

-- ===========================================================================
-- users: identity-provisioning columns and email uniqueness
-- ===========================================================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS scim_external_id TEXT;

-- How this account came to exist. Reporting only — never an authorization
-- input. Existing rows predate the column, so they are "unknown"; a later
-- backfill is not worth blocking on and would guess wrong.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS provisioned_by TEXT;

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_provisioned_by_check;
ALTER TABLE users
    ADD CONSTRAINT users_provisioned_by_check
    CHECK (provisioned_by IS NULL OR provisioned_by IN
        ('manual', 'invitation', 'signup', 'sso', 'scim', 'provisioning', 'bootstrap'));

-- Widen the provider enum for OIDC federated login. The original CHECK shipped
-- with aspirational values ('google', 'github', 'saml') that no code path
-- could write, because there was no login flow to write them from. There is
-- one now, and a generic provider name is the honest label: the tenant's
-- connection row says which IdP it was.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_provider_check;
ALTER TABLE users
    ADD CONSTRAINT users_provider_check
    CHECK (provider IN ('google', 'github', 'saml', 'local', 'basic', 'oidc'));

-- One account per email per tenant, for humans.
--
-- Scoped to the organization on purpose: a single instance can host unrelated
-- tenants, and the same person may legitimately hold an account in two of
-- them. Scoping wider would make one tenant's data collide with another's.
--
-- `LOWER(email)` because mail routing is case-insensitive in practice and
-- "Jane@x.com" vs "jane@x.com" would otherwise produce exactly the ambiguity
-- this index exists to remove.
--
-- The migration is defensive: it must not brick startup on an instance that
-- already holds duplicates. When a collision is found the oldest row keeps the
-- address and later rows have theirs NULLed and preserved in
-- provider_metadata so an operator can reconcile by hand. Losing a
-- non-authoritative display field is recoverable; refusing to boot is not.
DO $$
DECLARE
    dup RECORD;
    prior_metadata JSONB;
BEGIN
    FOR dup IN
        SELECT u.id, u.email, u.provider_metadata
        FROM users u
        WHERE u.email IS NOT NULL
          AND u.email <> ''
          AND u.role <> 'daemon'
          AND EXISTS (
              SELECT 1
              FROM users other
              WHERE other.organization_id = u.organization_id
                AND other.role <> 'daemon'
                AND LOWER(other.email) = LOWER(u.email)
                AND (
                    other.created_at < u.created_at
                    OR (other.created_at = u.created_at AND other.id < u.id)
                )
          )
    LOOP
        prior_metadata := COALESCE(dup.provider_metadata, '{}'::jsonb)
            || jsonb_build_object('duplicate_email_unlinked', dup.email);
        UPDATE users
        SET email = NULL,
            provider_metadata = prior_metadata,
            updated_at = NOW()
        WHERE id = dup.id;
        RAISE NOTICE 'Unlinked duplicate email % on user % (kept in provider_metadata)',
            dup.email, dup.id;
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_email_unique
    ON users (organization_id, LOWER(email))
    WHERE email IS NOT NULL AND email <> '' AND role <> 'daemon';

COMMENT ON INDEX idx_users_org_email_unique IS
    'Human members of one tenant have distinct email addresses. Required so that '
    'SSO and SCIM identity matching by email cannot be ambiguous.';

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_scim_external_id
    ON users (organization_id, scim_external_id)
    WHERE scim_external_id IS NOT NULL;

COMMENT ON INDEX idx_users_org_scim_external_id IS
    'A SCIM-managed user is addressable by the identity provider''s own id, '
    'scoped to one tenant so two tenants with the same IdP cannot collide.';

-- ===========================================================================
-- identity_invitations
-- ===========================================================================

CREATE TABLE IF NOT EXISTS identity_invitations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- Lowercased at rest. An invitation is addressed to one mailbox; anything
    -- else lets a leaked link be redeemed by a different person.
    email TEXT NOT NULL,

    -- Role granted on acceptance. Restricted by CHECK so a compromised or
    -- buggy write path cannot mint an invitation more powerful than an
    -- existing role.
    role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('member', 'admin', 'owner')),

    -- Only the SHA-256 of the token is stored, exactly like a session token:
    -- a database read must not yield a usable credential.
    token_hash TEXT NOT NULL UNIQUE,

    invited_by UUID REFERENCES users(id) ON DELETE SET NULL,
    note TEXT,

    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Single use. `accepted_at` doubles as the audit trail for "who got in".
    accepted_at TIMESTAMPTZ,
    accepted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    revoked_at TIMESTAMPTZ,

    CONSTRAINT identity_invitations_email_not_blank CHECK (email <> '')
);

CREATE INDEX IF NOT EXISTS idx_identity_invitations_org
    ON identity_invitations (organization_id, created_at DESC);

-- Partial index for the only hot query: "is this mailbox already invited?".
CREATE INDEX IF NOT EXISTS idx_identity_invitations_pending_email
    ON identity_invitations (organization_id, LOWER(email))
    WHERE accepted_at IS NULL AND revoked_at IS NULL;

COMMENT ON TABLE identity_invitations IS
    'Single-use, expiring invitation links. Only the token hash is stored.';

-- ===========================================================================
-- sso_connections
-- ===========================================================================

CREATE TABLE IF NOT EXISTS sso_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

provider_type TEXT NOT NULL CHECK (provider_type IN ('oidc', 'saml', 'github')),

    -- Stable, human-readable handle used in the login URL
    -- (`/sso/{slug}/start`), so a person can be told which button to press.
    slug TEXT NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    display_name TEXT NOT NULL,

    is_enabled BOOLEAN NOT NULL DEFAULT true,

    -- ── OIDC ────────────────────────────────────────────────────────────
    -- `issuer` is the trust anchor: discovery is fetched from it and the
    -- `iss` claim is compared against it after signature verification.
    issuer TEXT,
    client_id TEXT,
    -- The client secret is NOT here. It lives in the configured secret store
    -- under `identity.sso.<id>.client_secret`; a table dump must not leak it.
    scopes TEXT NOT NULL DEFAULT 'openid email profile',

    -- ── SAML ────────────────────────────────────────────────────────────
    idp_entity_id TEXT,
    idp_sso_url TEXT,
    -- JSON array of IdP signing certificates in PEM form. An array, not one
    -- value, because IdP certificate rollover is routine and a hard cutover
    -- drops every login between "new cert deployed" and "SP reconfigured".
    idp_certificates JSONB NOT NULL DEFAULT '[]'::jsonb,
    sp_entity_id TEXT,
    name_id_format TEXT NOT NULL DEFAULT
        'urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress',
    want_assertions_signed BOOLEAN NOT NULL DEFAULT true,
    want_response_signed BOOLEAN NOT NULL DEFAULT false,

    -- ── Shared behaviour ────────────────────────────────────────────────
    -- When set, only these mail domains may federate. This is the difference
    -- between "our IdP" and "any Google account on earth" for a shared IdP
    -- like Google Workspace, so it defaults to empty meaning "allow any" only
    -- because the tenant explicitly configures an IdP they control.
    allowed_email_domains JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- JIT provisioning: create the account on first successful assertion.
    -- Off means someone must invite or pre-create the user first.
    auto_create_users BOOLEAN NOT NULL DEFAULT true,

    -- Role granted to JIT-provisioned users. Never `hyperadmin`.
    default_role TEXT NOT NULL DEFAULT 'member'
        CHECK (default_role IN ('member', 'admin', 'owner')),

    -- JSON list of {idp_value, role} applied against a configured claim
    -- (usually `groups`) to derive a role. Highest matching role wins.
    role_mappings JSONB NOT NULL DEFAULT '[]'::jsonb,
    role_claim TEXT NOT NULL DEFAULT 'groups',

    -- JSON map of IdP claim name -> Lucent attribute. Lets a tenant whose IdP
    -- emits `mail` or `urn:oid:...` configure it without a code change.
    attribute_mapping JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Linking a federated identity to an existing local account by email is a
    -- privilege transfer. It is allowed by default (that is the point of SSO
    -- for an existing tenant) but a tenant can refuse it, and it always
    -- requires a verified address.
    allow_email_linking BOOLEAN NOT NULL DEFAULT true,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (organization_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_sso_connections_org
    ON sso_connections (organization_id);

COMMENT ON TABLE sso_connections IS
    'Per-tenant identity-provider configuration for OIDC and SAML federation.';

-- ===========================================================================
-- sso_login_states
-- ===========================================================================

CREATE TABLE IF NOT EXISTS sso_login_states (
    -- SHA-256 of the state/RelayState value sent to the IdP. The plaintext
    -- exists only in the redirect URL.
    state_hash TEXT PRIMARY KEY,
    connection_id UUID NOT NULL REFERENCES sso_connections(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- OIDC PKCE verifier and nonce. Null for SAML.
    code_verifier TEXT,
    nonce TEXT,

    -- SAML request id, echoed back as InResponseTo. Null for OIDC.
    saml_request_id TEXT,

    -- Post-login destination, constrained to a same-origin path at use time.
    return_to TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sso_login_states_expiry
    ON sso_login_states (expires_at);

COMMENT ON TABLE sso_login_states IS
    'Single-use, short-lived state for an in-flight SSO login. Consuming a row '
    'is the replay defence: a second callback with the same state finds nothing.';

-- ===========================================================================
-- saml_assertion_replays
-- ===========================================================================

CREATE TABLE IF NOT EXISTS saml_assertion_replays (
    assertion_id TEXT PRIMARY KEY,
    connection_id UUID NOT NULL REFERENCES sso_connections(id) ON DELETE CASCADE,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Kept until the assertion would have expired anyway.
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_saml_assertion_replays_expiry
    ON saml_assertion_replays (expires_at);

COMMENT ON TABLE saml_assertion_replays IS
    'Assertion IDs already accepted. SAML assertions are bearer tokens with no '
    'server-side session, so replay rejection requires remembering spent IDs '
    'for at least the assertion lifetime.';

-- ===========================================================================
-- scim_tokens
-- ===========================================================================

CREATE TABLE IF NOT EXISTS scim_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,

    -- Same convention as api_keys: a short plaintext prefix for lookup, and a
    -- bcrypt hash of the whole value for constant-time verification.
    token_prefix TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,

    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    use_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_scim_tokens_org
    ON scim_tokens (organization_id)
    WHERE revoked_at IS NULL;

COMMENT ON TABLE scim_tokens IS
    'Org-scoped bearer tokens for the SCIM 2.0 provisioning API. Separate from '
    'api_keys because a SCIM token grants directory mutation, which no user '
    'API key should imply.';
