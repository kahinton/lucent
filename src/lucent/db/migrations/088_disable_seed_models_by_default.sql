-- Migration 088: Disable shipped seed models by default.
-- Migration 031 seeded the catalog with hardcoded provider models, all enabled.
-- On instances where no model provider is configured, those rows still surfaced
-- in the chat model picker as "default offerings" the operator never set up.
-- Migration 072 made provider-discovered rows opt-in; this completes that policy
-- for the shipped seed rows so a fresh workspace shows no models until an admin
-- configures a provider and enables specific models. Admin-customized rows
-- (is_custom) and rows confirmed by provider discovery (discovery_source =
-- 'provider') are untouched.

UPDATE models
SET is_enabled = false,
    updated_at = NOW()
WHERE discovery_source = 'seed'
  AND is_custom = false
  AND is_enabled = true;
