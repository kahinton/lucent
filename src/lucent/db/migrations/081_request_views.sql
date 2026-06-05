-- Migration 081: Track per-user request detail views
-- Adds lightweight read-state for activity cards so completed work can be
-- highlighted until the viewer opens the request detail page after completion.

CREATE TABLE IF NOT EXISTS request_views (
    request_id UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    first_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (request_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_request_views_user_recent
    ON request_views(organization_id, user_id, last_viewed_at DESC);

COMMENT ON TABLE request_views IS
  'Per-user request detail view timestamps used to highlight completed work that has not been viewed after completion.';
COMMENT ON COLUMN request_views.last_viewed_at IS
  'Most recent time this user opened the request detail page.';
