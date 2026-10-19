-- Migration 140: Request-level execution pin
-- A request is the chat -> daemon handoff for durable work. The chat agent
-- knows the user's explicit engine/backend/model choice but cannot create
-- tasks itself, and free-text descriptions are not a reliable transport for a
-- UUID. These columns carry the pin structurally from authoring through
-- decomposition; tasks inherit them at insert time when they do not override
-- the value.
--
-- Nullable and additive: existing requests keep their current routing.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS backend_id UUID REFERENCES backends(id) ON DELETE SET NULL;

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS requested_model VARCHAR(128);

CREATE INDEX IF NOT EXISTS idx_requests_backend
    ON requests(backend_id)
    WHERE backend_id IS NOT NULL;
