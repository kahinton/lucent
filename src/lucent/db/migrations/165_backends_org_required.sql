-- Migration 165: Every backend is organization-owned (no shared backends).
--
-- !!! THIS MIGRATION RUNS AUTOMATICALLY AT SERVER STARTUP !!!
-- ----------------------------------------------------------
-- The API process applies pending migrations in ``init_db`` -> ``_run_migrations``
-- (``src/lucent/api/app.py``, ``src/lucent/db/pool.py``) before it serves
-- traffic; there is no separate migration job and there is no init container.
-- The runner gives each file its OWN transaction and does not swallow errors,
-- so a RAISE or a failed ``SET NOT NULL`` aborts the boot: if this migration
-- fails, the server does not come up. The totality asserts below run in the
-- SAME transaction as the backfill and ``SET NOT NULL``, so a partial fan-out
-- aborts this whole file rather than reaching ``SET NOT NULL`` with NULLs left
-- behind and taking startup down with an opaque constraint error.
--
-- What this changes
-- -----------------
-- Migration 136 introduced ``backends.organization_id`` as NULLABLE, with NULL
-- meaning "shared/global", plus ``UNIQUE NULLS NOT DISTINCT
-- (organization_id, engine, provider, name)``. A shared backend was visible to
-- every organization through ``BackendRepository.list``, which made a model
-- provider shareable across tenants. This migration makes an org-less backend
-- structurally impossible:
--   1. fan out every shared backend into one org-owned copy per organization,
--      preserving name, engine, provider, config, secret_refs, is_enabled and
--      created_at; each copy gets a fresh ``id``;
--   2. verify every (shared backend, organization) pair now has an org-owned
--      row, then delete the shared originals;
--   3. ``ALTER COLUMN organization_id SET NOT NULL``;
--   4. replace the NULLS NOT DISTINCT constraint with a plain UNIQUE.
--
-- Secret refs: option (b) -- fail loudly, never clone credentials
-- ---------------------------------------------------------------
-- ``secret_refs`` holds secret-key NAMES, not values (migration 136). Secret
-- resolution is strictly per-organization: ``secrets.organization_id`` is NOT
-- NULL and every lookup filters by the caller's org, so a clone in org X
-- resolving key ``engine.codex.<name>.api_key`` searches X's scope, finds
-- nothing, and falls back to environment/config -- never to the source
-- backend's value. (Inventory-verified in ``langchain_engine._backend_secret``,
-- which uses the caller org, and ``model_discovery._backend_api_key``, which
-- uses the backend row's org.) A SQL migration cannot create per-org
-- credentials. Cloning non-empty ``secret_refs`` would therefore silently
-- produce rows whose credentials are unreadable, which this migration refuses
-- to do.
--
-- Chosen resolution: the first block below RAISES if any shared backend carries
-- a non-empty ``secret_refs``, and the message tells the operator exactly what
-- to do (create the referenced keys in every org, or clear ``secret_refs`` if
-- env/config fallback is intended). The two shared rows present today both
-- carry ``'{}'``, so the guard passes and the fan-out runs. Failing the boot is
-- the correct loud failure here: leaving those rows out would leave NULLs that
-- ``SET NOT NULL`` rejects anyway, with a far less actionable error.
--
-- Collisions: prefer the organization's existing row (skip the clone)
-- ---------------------------------------------------------------------
-- The same ``UNIQUE NULLS NOT DISTINCT (organization_id, engine, provider,
-- name)`` means a clone can collide with a row the org already owns under that
-- (engine, provider, name). Resolution: SKIP, via ``ON CONFLICT DO NOTHING``.
-- The org's existing row is authoritative -- it may already carry
-- org-specific config/secret_refs the shared row did not, so overwriting it
-- would change what the org is actually using, and renaming the clone would
-- invent a backend the org never chose. The coverage assert that follows then
-- proves every (org, shared backend) pair is satisfied by SOME row with that
-- key before the originals are deleted, so a skipped clone cannot leave an org
-- without a working backend.
--
-- Pinned rows: the DELETE detaches backends (FK ON DELETE SET NULL)
-- ----------------------------------------------------------------
-- Three tables reference ``backends`` with ON DELETE SET NULL:
-- ``models.backend_id``, ``requests.backend_id`` and ``tasks.backend_id``.
-- Deleting the shared rows therefore nulls those pins. For model rows this is
-- behavior-preserving: the shared backends carry empty ``config`` and
-- ``secret_refs``, and ``factory.get_engine_for_model`` has a complete
-- provider -> default-engine path driven by ``models.engine`` /
-- ``models.provider``, so resolution is unchanged. Request and task pins
-- resolve through the same path once nulled, and in-flight work can be
-- re-pinned. Repointing is deliberately NOT done here: global
-- (``organization_id IS NULL``) models have no org clone to point at, and
-- rewriting only the org-scoped half would be a partial, unrelated data
-- change. An operator who wants the pins preserved can remap them to a
-- specific org's clone after this migration commits.
--
-- Transaction and rollback
-- ------------------------
-- No explicit BEGIN/COMMIT: the runner (``_run_migrations``) opens one
-- transaction per file, so the guards, fan-out, asserts, DELETE, SET NOT NULL
-- and constraint swap all commit or roll back together.
-- ``165_backends_org_required.down.sql`` restores the nullable column and the
-- NULLS NOT DISTINCT constraint. It does not restore the deleted shared rows;
-- see that file for what the rollback can and cannot undo.

-- Guard 1: no shared backend may carry secret references (option (b)).
DO $guard_secret_refs$
DECLARE
    offending integer;
BEGIN
    SELECT count(*) INTO offending
      FROM backends
     WHERE organization_id IS NULL
       AND secret_refs IS DISTINCT FROM '{}'::jsonb;

    IF offending <> 0 THEN
        RAISE EXCEPTION
            'migration 165: % shared backend(s) carry non-empty secret_refs. '
            'Secrets are per-organization (secrets.organization_id NOT NULL), '
            'so a clone resolves the same key NAMES in its own org and finds '
            'nothing, silently falling back to env/config instead of the shared '
            'backend''s credential. This migration cannot create credentials. '
            'Before applying: for each affected backend, either (1) create the '
            'referenced secret key(s) in EVERY organization, or (2) clear '
            'secret_refs if env/config fallback is intended. Aborting; no '
            'backends were changed.',
            offending
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
END;
$guard_secret_refs$;

-- Guard 2: a fan-out targets existing organizations; with zero organizations
-- there is nowhere to put the copies and the DELETE below would destroy them.
DO $guard_organizations_exist$
BEGIN
    IF EXISTS (SELECT 1 FROM backends WHERE organization_id IS NULL)
       AND NOT EXISTS (SELECT 1 FROM organizations) THEN
        RAISE EXCEPTION
            'migration 165: shared backend row(s) exist but there are no '
            'organizations to fan them out into; refusing to delete the shared '
            'rows. Create at least one organization, or clear the shared '
            'backends deliberately, before applying.'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
END;
$guard_organizations_exist$;

-- Backfill: one org-owned copy per (shared backend, organization).
-- A fresh ``id`` comes from the column default; ``created_at`` is carried over
-- verbatim and ``updated_at`` takes the default because the row is new.
INSERT INTO backends (
    organization_id, name, engine, provider,
    config, secret_refs, is_enabled, created_at
)
SELECT
    o.id,
    s.name,
    s.engine,
    s.provider,
    s.config,
    s.secret_refs,
    s.is_enabled,
    s.created_at
FROM backends s
CROSS JOIN organizations o
WHERE s.organization_id IS NULL
ON CONFLICT (organization_id, engine, provider, name) DO NOTHING;

-- Guard 3: the fan-out must be total for every (org, shared backend) pair
-- before the originals are removed. A pair is satisfied either by a copy just
-- inserted or by the org's pre-existing same-key row (the SKIP resolution).
DO $assert_fanout_total$
DECLARE
    uncovered integer;
BEGIN
    SELECT count(*) INTO uncovered
      FROM backends s
      CROSS JOIN organizations o
     WHERE s.organization_id IS NULL
       AND NOT EXISTS (
           SELECT 1
             FROM backends c
            WHERE c.organization_id = o.id
              AND c.engine = s.engine
              AND c.provider = s.provider
              AND c.name = s.name
       );

    IF uncovered <> 0 THEN
        RAISE EXCEPTION
            'migration 165: % (shared backend, organization) pair(s) have no '
            'org-owned row after the fan-out; refusing to delete the shared '
            'rows. Inspect with: SELECT s.id, s.name, s.engine, s.provider, '
            'o.id AS org_id FROM backends s CROSS JOIN organizations o '
            'WHERE s.organization_id IS NULL AND NOT EXISTS (SELECT 1 FROM '
            'backends c WHERE c.organization_id = o.id AND c.engine = s.engine '
            'AND c.provider = s.provider AND c.name = s.name);',
            uncovered
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
END;
$assert_fanout_total$;

-- Remove the shared originals only after every org-owned copy exists.
DELETE FROM backends WHERE organization_id IS NULL;

-- Guard 4: literally assert zero NULL-org rows immediately before the
-- constraint that would otherwise abort startup. Catches a concurrent writer
-- racing this transaction as well as any unexpected leftover.
DO $assert_no_null_org$
DECLARE
    remaining integer;
BEGIN
    SELECT count(*) INTO remaining FROM backends WHERE organization_id IS NULL;

    IF remaining <> 0 THEN
        RAISE EXCEPTION
            'migration 165: % backend row(s) still have organization_id IS NULL '
            'after deleting the shared rows; refusing to SET NOT NULL (a NULL '
            'row would abort startup). Inspect with: SELECT id, name, engine, '
            'provider FROM backends WHERE organization_id IS NULL;',
            remaining
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
END;
$assert_no_null_org$;

-- No org-less backend from here on.
ALTER TABLE backends
    ALTER COLUMN organization_id SET NOT NULL;

-- Replace the NULLS NOT DISTINCT uniqueness with a plain UNIQUE. The original
-- allowed exactly one NULL-org row per (engine, provider, name); NULL is no
-- longer a valid value, so the distinction has no meaning. DROP-then-ADD keeps
-- the established name so ON CONFLICT inference (and the BackendRepository
-- upsert path) continue to resolve it.
ALTER TABLE backends
    DROP CONSTRAINT IF EXISTS backends_organization_id_engine_provider_name_key;

ALTER TABLE backends
    ADD CONSTRAINT backends_organization_id_engine_provider_name_key
    UNIQUE (organization_id, engine, provider, name);
