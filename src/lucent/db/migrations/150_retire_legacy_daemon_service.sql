-- Migration 150: Retire the bare `daemon-service` identity.
--
-- Background
-- ----------
-- Migration 017 introduced a single, instance-wide `daemon-service` user so the
-- daemon had something to authenticate as. Multi-tenancy later replaced it with
-- one identity per organization (`daemon-service:{org_id}`), provisioned by
-- `OrganizationRepository.create()`. Both forms were then recognized everywhere:
-- nine code paths matched either `external_id = 'daemon-service'` or the
-- `daemon-service:` prefix.
--
-- That left a dormant privilege path. The bare account cannot authenticate — it
-- has no password and, on the live instance, zero API keys — yet it still held
-- 1,248 memory read clearances (unscoped daemon keys are documented as "acting
-- as the org's first owner" for memory visibility) plus owner clearances on 40
-- definitions. Nothing prevented minting a key against it and inheriting
-- owner-equivalent reach by *name prefix* rather than by role or clearance.
--
-- What this migration does
-- ------------------------
-- 1. Move clearances off the legacy account:
--      * grants on built-in definitions (source of truth on disk, i.e.
--        `.github/skills/`) go to the organization's own daemon identity;
--      * everything else goes to the human owner, because those are personal
--        or historical grants rather than system ones.
--    Exact duplicates (same auth_id + role already held by the new principal)
--    are deleted instead, to respect `auth_clearances`' unique constraint.
-- 2. Deactivate the legacy account so it can never authenticate, and blank any
--    credential material it might somehow carry.
--
-- Why it is deactivated rather than deleted
-- -----------------------------------------
-- ~19,200 historical rows still reference the identity through `NO ACTION`
-- foreign keys — 9,801 `tasks.requesting_user_id`, 4,632
-- `requests.created_by`, and 4,614 `tool_call_audit_log.user_id`. Deleting the
-- user row would require rewriting the authorship of years of work and audit
-- history, which is precisely the record we do not want to falsify. The account
-- has no way to authenticate once deactivated, so the security objective is met
-- without destroying provenance. The row now exists only as
-- "an account that once did this work".

DO $$
DECLARE
    legacy_id   UUID;
    org_daemon  UUID;
    owner_id    UUID;
    moved_sys   INTEGER := 0;
    moved_owner INTEGER := 0;
    dropped     INTEGER := 0;
BEGIN
    SELECT id INTO legacy_id FROM users WHERE external_id = 'daemon-service' LIMIT 1;
    IF legacy_id IS NULL THEN
        RAISE NOTICE 'Legacy daemon-service identity not present; nothing to do.';
        RETURN;
    END IF;

    -- The org daemon that inherits system grants: the identity for the
    -- organization the legacy account belongs to.
    SELECT u.id INTO org_daemon
    FROM users u
    WHERE u.external_id = 'daemon-service:' || (
              SELECT organization_id::text FROM users WHERE id = legacy_id
          )
    LIMIT 1;

    -- The human owner, used for everything that is not a system grant. Prefer
    -- an owner in the same organization; fall back to the earliest owner.
    SELECT u.id INTO owner_id
    FROM users u
    WHERE u.role = 'owner'
      AND u.is_active
      AND u.external_id NOT LIKE 'daemon-service:%'
      AND u.organization_id = (SELECT organization_id FROM users WHERE id = legacy_id)
    ORDER BY u.created_at
    LIMIT 1;

    IF owner_id IS NULL THEN
        SELECT u.id INTO owner_id
        FROM users u
        WHERE u.role = 'owner'
          AND u.is_active
          AND u.external_id NOT LIKE 'daemon-service:%'
        ORDER BY u.created_at
        LIMIT 1;
    END IF;

    -- 1a. System grants (built-in definitions) -> the organization's daemon.
    IF org_daemon IS NOT NULL THEN
        WITH builtin_auth AS (
            SELECT auth_id FROM skill_definitions    WHERE name = 'security-audit'
            UNION SELECT auth_id FROM agent_definitions   WHERE scope = 'built-in'
            UNION SELECT auth_id FROM skill_definitions   WHERE scope = 'built-in'
            UNION SELECT auth_id FROM sandbox_templates   WHERE scope = 'built-in'
            UNION SELECT auth_id FROM mcp_server_configs  WHERE scope = 'built-in'
        ),
        moved AS (
            UPDATE auth_clearances ac
            SET principal_id = org_daemon
            WHERE ac.principal_type = 'user'
              AND ac.principal_id = legacy_id
              AND ac.auth_id IN (SELECT auth_id FROM builtin_auth)
              AND NOT EXISTS (
                  SELECT 1 FROM auth_clearances k
                  WHERE k.auth_id = ac.auth_id
                    AND k.principal_type = 'user'
                    AND k.principal_id = org_daemon
                    AND k.role = ac.role
              )
            RETURNING 1
        )
        SELECT count(*) INTO moved_sys FROM moved;
    END IF;

    -- 1b. Everything else -> the human owner.
    IF owner_id IS NOT NULL THEN
        WITH moved AS (
            UPDATE auth_clearances ac
            SET principal_id = owner_id
            WHERE ac.principal_type = 'user'
              AND ac.principal_id = legacy_id
              AND NOT EXISTS (
                  SELECT 1 FROM auth_clearances k
                  WHERE k.auth_id = ac.auth_id
                    AND k.principal_type = 'user'
                    AND k.principal_id = owner_id
                    AND k.role = ac.role
              )
            RETURNING 1
        )
        SELECT count(*) INTO moved_owner FROM moved;
    END IF;

    -- 1c. Whatever remains is a duplicate of a grant the new principal already
    --     holds, or has no eligible recipient. Either way it must not stay
    --     attached to a retired identity.
    WITH gone AS (
        DELETE FROM auth_clearances
        WHERE principal_type = 'user' AND principal_id = legacy_id
        RETURNING 1
    )
    SELECT count(*) INTO dropped FROM gone;

    -- 2. Make the identity permanently unable to authenticate.
    UPDATE users
    SET is_active = false,
        password_hash = NULL,
        force_password_change = false,
        updated_at = NOW()
    WHERE id = legacy_id;

    RAISE NOTICE
        'Retired daemon-service %: % grant(s) -> org daemon, % -> owner, % duplicate(s) dropped',
        legacy_id, moved_sys, moved_owner, dropped;
END $$;

COMMENT ON COLUMN users.external_id IS
    'Identity id. Daemon service accounts are per-organization: '
    'daemon-service:{org_id}. The bare legacy ''daemon-service'' value is retired '
    '(inactive, no credentials) and is no longer recognized by application code.';
