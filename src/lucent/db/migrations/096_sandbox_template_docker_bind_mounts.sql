-- Migration 096: Persist Docker-only bind mounts on portable sandbox templates.

ALTER TABLE sandbox_templates
ADD COLUMN IF NOT EXISTS docker_bind_mounts JSONB NOT NULL DEFAULT '[]'::jsonb;
