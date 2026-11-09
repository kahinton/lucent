-- Migration 164: Organization entitlements.
--
-- Background
-- ----------
-- Two organizations on the same deployment can currently do identical work:
-- both may propose and use sandbox templates, and both may create, grant and
-- run managed tools. The only existing knob is a runtime setting
-- (`features.sandboxes_enabled`, read through `settings.sandboxes_enabled` /
-- `settings.custom_tooling_enabled`), which is configuration, not a
-- per-organization commitment — it cannot say "this org's plan includes
-- managed tools", it cannot be listed per org, and it has no audit trail.
--
-- This migration adds the *entitlement* half of a plan system: one row per
-- (organization, capability) that records whether the capability is enabled,
-- and, in `limits`, whatever shape-specific numbers the capability later
-- decides to honour (concurrent sandboxes, exec timeouts, tool run quotas).
--
-- What this migration deliberately does NOT add
-- ---------------------------------------------
-- A plan catalogue. "Pro", "Team" and pricing are later steps, and a plan
-- table created now would have no reader: nothing in the application knows
-- what a plan is, so a tier name could not be resolved to entitlement rows
-- without inventing the reader in the same change. The capability rows in
-- `organization_entitlements` are the contract everything else builds on; the
-- plans table, when it arrives, will set these rows rather than be consulted
-- per request.
--
-- The `source` column keeps the story visible
-- -------------------------------------------
-- Entitlements can be arrived at through different authority, and when an
-- entitlement stops working it matters which:
--   * `plan`          — the org is on some plan; enabled by plan assignment
--                       machinery that does not exist yet (source stays the
--                       default until then).
--   * `override`      — flip-flopped directly by an administrator, e.g.
--                       "managed-tools for this org for the eval", recorded
--                       in the row itself rather than in a chat log.
--   * `grandfathered` — the migration backfill below. Existing orgs are
--                       enabled with this source so the backfill is visible
--                       and the pre-164 default can be reasoned about later.
--
-- Enforcement is NOT in this migration. Nothing reads these rows at request
-- time yet; the next change gates sandbox-template and managed-tool dispatch
-- on them. After the gate ships, a missing row means *denied*, which the
-- CHECK/unique design below makes representable: there is no row, so there
-- is no entitlement.

CREATE TABLE IF NOT EXISTS organization_entitlements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    -- Capability the row grants. Constrained below so a typo cannot create a
    -- silently dead row (a row with capability 'managed_tols' would insert,
    -- read as "managed tools not granted", and never be noticed).
    capability VARCHAR(64) NOT NULL,
    is_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    -- Where the entitlement came from. Constraining the value keeps the
    -- sources list authoritative: a new source requires being named in the
    -- check rather than being invented by a form typo.
    source VARCHAR(16) NOT NULL DEFAULT 'plan',
    -- Capability-specific limits. Shape belongs to the capability, not to the
    -- schema, because each capability evolves on its own schedule; any
    -- consumer that reads specific keys validates them in application code.
    limits JSONB NOT NULL DEFAULT '{}'::jsonb,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE organization_entitlements
    DROP CONSTRAINT IF EXISTS organization_entitlements_capability_check;
ALTER TABLE organization_entitlements
    ADD CONSTRAINT organization_entitlements_capability_check
    CHECK (capability IN ('custom_sandboxes', 'managed_tools'));

ALTER TABLE organization_entitlements
    DROP CONSTRAINT IF EXISTS organization_entitlements_source_check;
ALTER TABLE organization_entitlements
    ADD CONSTRAINT organization_entitlements_source_check
    CHECK (source IN ('plan', 'override', 'grandfathered'));

-- One row per org per capability. Upserts (EntitlementRepository
-- .set_entitlement) key on this; a second row for the same pair would make
-- "is_enabled" ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_organization_entitlements_org_capability
    ON organization_entitlements (organization_id, capability);

-- Enforcement reads: the check is "is this (org, capability) enabled", one
-- indexed lookup per gate site; the bulk list for the admin control plane
-- scans by organization_id.
CREATE INDEX IF NOT EXISTS idx_organization_entitlements_org
    ON organization_entitlements (organization_id);

-- updated_at is maintained the same way as users (migration 002) and
-- organizations (migration 003) — the shared trigger from migration 001.
DROP TRIGGER IF EXISTS update_organization_entitlements_updated_at
    ON organization_entitlements;
CREATE TRIGGER update_organization_entitlements_updated_at
    BEFORE UPDATE ON organization_entitlements
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Backfill
-- --------
-- Every organization that exists at migration time gets both capabilities
-- ENABLED. This is what keeps the current instance working: before 164 there
-- was no entitlement table, and orgs simply did what they did — a gate that
-- ships on top of an empty table would deny everything silently, which is a
-- far worse failure than over-granting and letting an administrator turn rows
-- off.
--
-- The system organizations (`__lucent_system__` and its future kin, all
-- conventionally named with a leading `__lucent`) are infrastructure orgs,
-- not customers: they hold system secrets and daemon identities. Granting
-- them a "plan" entitlement would be inventing a customer that does not
-- exist, so they are excluded — like every org created after the migration,
-- they can be entitled explicitly with set_entitlement if ever needed
-- (source 'override').
INSERT INTO organization_entitlements
    (organization_id, capability, is_enabled, source, limits, note)
SELECT
    o.id,
    c.capability,
    TRUE,
    'grandfathered',
    '{}'::jsonb,
    'Enabled by migration 164 backfill: capabilities were unrestricted before'
FROM organizations o
CROSS JOIN (VALUES ('custom_sandboxes'), ('managed_tools')) AS c(capability)
WHERE o.name NOT LIKE '\_\_lucent%'
ON CONFLICT (organization_id, capability) DO NOTHING;
