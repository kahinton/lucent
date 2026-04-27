-- Migration 071: Model discovery metadata
-- Adds source tracking so provider discovery can refresh catalog rows without
-- overwriting manually-added custom models.

ALTER TABLE models ADD COLUMN IF NOT EXISTS discovery_source VARCHAR(16) NOT NULL DEFAULT 'seed';
ALTER TABLE models ADD COLUMN IF NOT EXISTS is_custom BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE models ADD COLUMN IF NOT EXISTS last_discovered_at TIMESTAMPTZ DEFAULT NULL;
ALTER TABLE models ADD COLUMN IF NOT EXISTS discovery_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_models_discovery_source ON models(discovery_source);
CREATE INDEX IF NOT EXISTS idx_models_last_discovered ON models(last_discovered_at);

-- Preserve rows that were manually created before this migration. A row with an
-- organization_id is instance-scoped rather than a shipped seed row.
UPDATE models
SET discovery_source = 'manual', is_custom = true
WHERE organization_id IS NOT NULL
  AND discovery_source = 'seed';
