-- Migration 130: Clearance-representation backfills for built-in / system rows.
--
-- A drift audit across every migrated family found exactly one functional
-- gap: migration 126 added the owner-clearance INSERT/UPDATE triggers and
-- the built-in org-read + group-read backfills to managed_tool_definitions,
-- but never backfilled owner clearances for rows whose `owner_user_id` was
-- already set (123 skipped this table entirely, so those rows predate the
-- triggers). Result: every managed tool row in the dev DB has an owner but
-- zero clearances, so the clearance-driven *use* lookups
-- (`get_usable_managed_tool*`) deny access to everyone — including the
-- owner — and callers silently fall back to legacy org joins.
--
-- Everything else found by the audit is by design and stays as-is:
--   * models: zero-clearance rows are all disabled catalog entries —
--     default-deny until adopted; enabled models carry org/read grants.
--   * secrets: `system_managed = true` rows carry no clearances by design
--     (their reads go through the scoped system path).
--   * sandboxes: zero-clearance rows are April-era destroyed/failed
--     artifacts; 127's owner backfill covered everything live.
--   * schedules: is_system rows are represented by daemon-service-user
--     ownership (128 §2/§5 + the 123 created_by trigger); no org-wide
--     grant, because members must not see or run system schedules. The
--     single zero-clearance row lives in the legacy `__lucent_system__`
--     org, which has no human or daemon-service users at all — it is
--     process-owned infra and only ever read on the system contract.
--
-- Section 1 sets orphaned instance rows (owner_user_id IS NULL) to the
-- org's owner user; the 126 transfer trigger then files their clearances.
-- Section 2 backfills owner clearances for rows already carrying an owner.
-- Both are idempotent (ON CONFLICT DO NOTHING).

-- ---------------------------------------------------------------------------
-- 1. Safety: orphan instance rows (no user owner, no group owner) attach to
-- the org's first owner user — same pattern as 126 §5.
-- ---------------------------------------------------------------------------
UPDATE managed_tool_definitions t
SET owner_user_id = org.owner_user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
) AS org
WHERE t.scope = 'instance'
  AND t.owner_user_id IS NULL
  AND t.owner_group_id IS NULL
  AND t.organization_id = org.organization_id;

-- ---------------------------------------------------------------------------
-- 2. Backfill owner clearances for every managed tool that has an owner. This
-- is the gap 126 left: triggers existed, backfill didn't.
-- ---------------------------------------------------------------------------
INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id)
SELECT t.auth_id, 'owner', 'user', t.owner_user_id
FROM public.managed_tool_definitions t
WHERE t.owner_user_id IS NOT NULL
  AND t.auth_id IS NOT NULL
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

-- Expected drift after section 2: 0 rows with an owner but no clearance.
