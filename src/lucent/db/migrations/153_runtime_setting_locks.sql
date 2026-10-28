-- Migration 153: Instance-operator locks on runtime settings.
--
-- Background
-- ----------
-- Runtime settings (`runtime_settings`, 086) are per-organization and editable
-- by that organization's admin or owner. That is the right default for a single
-- team, but it makes two instance-level jobs impossible:
--
--   * **Holding a limit or a feature at a known value** for one tenant — "this
--     plan gets no sandboxes", "this pilot is capped at model X" — while the
--     tenant keeps its own settings screen.
--   * **Selling different capabilities at different tiers.** A tier is, in
--     practice, a set of settings that the customer must not be able to change.
--
-- A separate table rather than a column on `runtime_settings` because a lock is
-- not a property of a value: it must be expressible for a setting that has no
-- saved row at all. An operator can lock `features.sandboxes_enabled` while the
-- effective value still comes from the environment or the built-in default, and
-- a tenant resetting (deleting) the row must not silently drop the constraint.
-- A lock on a missing row is meaningful; a column on a missing row does not
-- exist.
--
-- What a lock is
-- --------------
-- It is a **write barrier**, not a stored value. The value itself stays in
-- `runtime_settings`, so there is exactly one source of truth and the lock can
-- never disagree with it. An operator who wants a tenant pinned to a value sets
-- the value and then locks it; unlocking never silently changes the effective
-- value, it only hands control back to the tenant.
--
-- `organization_id` and `key` are both free of foreign keys into the settings
-- catalog because the catalog lives in code (`lucent.settings`), not the
-- database. `organization_id` cascades with the tenant, so deleting an
-- organization takes its locks with it.
--
-- `locked_by` is `ON DELETE SET NULL`: the lock is instance policy and must
-- outlive the departure of the operator who applied it. The audit log holds the
-- attribution in that case.
--
-- Enforcement
-- -----------
-- Application-level, in `RuntimeSettingsRepository.upsert_setting` /
-- `delete_setting`, which read this table inside the same connection as the
-- write. It is deliberately not enforced from the in-process settings cache:
-- that cache is per-process, so a lock applied through one replica (or the ops
-- console) would not be visible to another replica and the barrier would be
-- advisory rather than real.
--
-- Additive and non-breaking: the table starts empty, so every existing tenant
-- stays fully self-service until an operator locks something.

CREATE TABLE IF NOT EXISTS runtime_setting_locks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    -- Free-text explanation shown to the tenant in place of the controls, so a
    -- customer sees "why" rather than a silently broken settings screen.
    reason TEXT NOT NULL DEFAULT '',
    -- The instance operator who applied the lock. NULL after that account is
    -- removed; the audit trail retains the attribution.
    locked_by UUID REFERENCES users(id) ON DELETE SET NULL,
    locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (organization_id, key)
);

COMMENT ON TABLE runtime_setting_locks IS
    'Instance-operator write barriers on org runtime settings. The value lives in runtime_settings; a row here only forbids the tenant from changing it.';
COMMENT ON COLUMN runtime_setting_locks.key IS
    'Stable allowlisted setting key from lucent.settings, matching runtime_settings.key.';
COMMENT ON COLUMN runtime_setting_locks.reason IS
    'Operator-supplied explanation rendered to the tenant in place of the editing controls.';

-- Reads are always "the locks for one organization", and the write path is a
-- point lookup on (organization_id, key) which the UNIQUE constraint already
-- indexes. This partial index serves the operator's cross-tenant view, where the
-- question is "what is locked anywhere" rather than "may this be changed".
CREATE INDEX IF NOT EXISTS idx_runtime_setting_locks_org
    ON runtime_setting_locks (organization_id);

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
    EXECUTE 'GRANT SELECT ON runtime_setting_locks TO lucent_daemon';
    RAISE NOTICE 'Granted daemon role SELECT on runtime_setting_locks';
  ELSE
    RAISE NOTICE 'lucent_daemon role not found — skipping runtime_setting_locks grant';
  END IF;
END $$;
