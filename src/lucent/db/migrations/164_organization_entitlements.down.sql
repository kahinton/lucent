-- Down migration for 164: remove organization entitlements.
--
-- The table exists solely as entitlement configuration for the
-- organization-level capability gate; nothing else reads it. Dropping it
-- removes any override/grandfathered rows, which is the intent of rolling
-- back: the pre-164 world had no entitlements at all, and capabilities were
-- ungoverned. The sandbox_templates/managed_tools data itself is untouched.

DROP TRIGGER IF EXISTS update_organization_entitlements_updated_at
    ON organization_entitlements;
DROP TABLE IF EXISTS organization_entitlements;
