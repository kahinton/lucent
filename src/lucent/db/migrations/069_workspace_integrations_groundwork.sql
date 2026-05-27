-- Migration 069: Workspace integrations groundwork for app/bot installs
--
-- Extends the existing ``integrations`` table (migration 028) so it can
-- represent first-class app installations such as a GitHub App, in
-- addition to the existing Slack/Discord bot rows.
--
-- This migration is constraint+column additive only. It does NOT touch
-- existing rows: the new columns are nullable, the type CHECK constraint
-- is widened (never narrowed), and only new indexes are introduced.
--
-- Design: two-tier connections (workspace integrations vs personal
-- enterprise_credentials). See connection_flags.py.

-- ---------------------------------------------------------------------------
-- 1. Widen the type CHECK to allow new app/bot install kinds.
-- ---------------------------------------------------------------------------
-- Existing constraint allowed only ('slack', 'discord'). We add
-- github_app, jira, linear, and a forward-compat 'custom' bucket. Drop
-- the old constraint by name then re-add the widened version.

ALTER TABLE integrations
    DROP CONSTRAINT IF EXISTS integrations_type_check;

ALTER TABLE integrations
    ADD CONSTRAINT integrations_type_check
    CHECK (type IN ('slack', 'discord', 'github_app', 'jira', 'linear', 'custom'));

-- ---------------------------------------------------------------------------
-- 2. Add columns describing app-install identity and health.
-- ---------------------------------------------------------------------------
-- ``install_id``      — provider-issued installation identifier
--                        (e.g. GitHub App installation_id). Nullable so
--                        existing Slack/Discord rows are unaffected.
-- ``health_status``   — most recent observed health: 'unknown',
--                        'healthy', 'degraded', or 'failed'.
-- ``health_detail``   — short human-readable diagnostic string.
-- ``health_checked_at`` — timestamp of the last health probe.

ALTER TABLE integrations
    ADD COLUMN IF NOT EXISTS install_id TEXT;

ALTER TABLE integrations
    ADD COLUMN IF NOT EXISTS health_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK (health_status IN ('unknown', 'healthy', 'degraded', 'failed'));

ALTER TABLE integrations
    ADD COLUMN IF NOT EXISTS health_detail TEXT;

ALTER TABLE integrations
    ADD COLUMN IF NOT EXISTS health_checked_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- 3. Indexes.
-- ---------------------------------------------------------------------------
-- One active install per (org, type, install_id) — only enforced when
-- install_id is present, so existing Slack/Discord rows (NULL install_id)
-- are not affected by this constraint.

CREATE UNIQUE INDEX IF NOT EXISTS idx_integrations_active_install
    ON integrations (organization_id, type, install_id)
    WHERE status = 'active' AND install_id IS NOT NULL;

-- Look up by install_id alone for webhook routing (e.g. GitHub App
-- delivery → installation_id → integration row).
CREATE INDEX IF NOT EXISTS idx_integrations_install_id
    ON integrations (install_id)
    WHERE install_id IS NOT NULL;
