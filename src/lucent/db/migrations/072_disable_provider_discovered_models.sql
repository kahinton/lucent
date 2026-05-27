-- Migration 072: Disable provider-discovered models by default.
-- Provider catalogs can expose high-cost, preview, or internal models. Keep
-- discovered rows opt-in so admins control cost and token usage explicitly.

UPDATE models
SET is_enabled = false,
    updated_at = NOW()
WHERE discovery_source = 'provider'
  AND is_custom = false
  AND is_enabled = true;