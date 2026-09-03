-- Migration 109: Persist extra hosts (--add-host entries) on sandbox templates.
--
-- Values are either a literal IP (e.g. "10.0.0.5") or the special value
-- "host-gateway", which the Docker backend resolves to the Docker Engine's
-- host-gateway IP at container creation (plain Linux Engine never injects the
-- host.docker.internal alias automatically, unlike Docker Desktop).

ALTER TABLE sandbox_templates
ADD COLUMN IF NOT EXISTS extra_hosts JSONB NOT NULL DEFAULT '{}'::jsonb;