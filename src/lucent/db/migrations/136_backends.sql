-- Migration 136: First-class engine backends
-- A ``backend`` is one engine + one provider family + one credential/config
-- binding. It generalizes the implicit (provider, engine, provider-key) triple
-- into a row so coding engines (Copilot today; Claude Code / Codex later) have
-- a place to carry BYOK / gateway / OSS config and a per-organization
-- credential binding.
--
-- Additive and safe to apply before the code that reads it lands: models and
-- tasks keep working without a backend (they fall back to the configured
-- default engine), and existing rows are backfilled to a behavior-preserving
-- shared backend.

CREATE TABLE IF NOT EXISTS backends (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- NULL = shared/global; a backend that binds an org credential is org-scoped.
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name            VARCHAR(128) NOT NULL,           -- admin label
    engine          VARCHAR(32)  NOT NULL,           -- 'langchain'|'copilot'|'claude_code'|'codex'
    provider        VARCHAR(32)  NOT NULL,           -- family: 'ollama'|'openai'|'anthropic'|'google'|'custom'
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,   -- NON-secret: base_url, wire_api, oss_provider, gateway_url
    secret_refs     JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {"api_key":"engine.codex.<name>.api_key"} — key NAMES only
    is_enabled      BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- NULLS NOT DISTINCT so shared (organization_id IS NULL) backends are also
    -- unique on (engine, provider, name) — a plain UNIQUE treats NULLs as distinct.
    UNIQUE NULLS NOT DISTINCT (organization_id, engine, provider, name)
);

CREATE INDEX IF NOT EXISTS idx_backends_org_engine ON backends(organization_id, engine);
CREATE INDEX IF NOT EXISTS idx_backends_engine_provider ON backends(engine, provider);

-- Models reference a default backend (nullable).
ALTER TABLE models
    ADD COLUMN IF NOT EXISTS backend_id UUID REFERENCES backends(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_models_backend ON models(backend_id);

-- Per-task / per-workflow pinning, mirroring tasks.decision_config / tasks.fusion_config.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS backend_id UUID REFERENCES backends(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_backend ON tasks(backend_id);

-- Backfill: one shared backend per distinct (provider, effective engine) that is
-- determinable from existing rows. Ollama models are always served by the
-- general LLM engine; other providers get a backend only when they pin an
-- engine. Cloud models with no engine override keep backend_id NULL so they
-- resolve through the configured default engine exactly as before.
INSERT INTO backends (organization_id, name, engine, provider)
SELECT NULL,
       s.provider || ' via ' || s.engine,
       s.engine,
       s.provider
FROM (
    SELECT DISTINCT provider,
           COALESCE(
               NULLIF(engine, ''),
               CASE WHEN provider = 'ollama' THEN 'langchain' END
           ) AS engine
    FROM models
) s
WHERE s.engine IS NOT NULL
ON CONFLICT DO NOTHING;

UPDATE models m
SET backend_id = b.id
FROM backends b
WHERE m.backend_id IS NULL
  AND b.organization_id IS NULL
  AND b.provider = m.provider
  AND b.engine = COALESCE(
        NULLIF(m.engine, ''),
        CASE WHEN m.provider = 'ollama' THEN 'langchain' END
      );
