-- Migration 151: Record which engines report a model in their own catalog.
--
-- Background
-- ----------
-- A model id is not namespaced by provider. GitHub Copilot and OpenAI Codex
-- both report `gpt-5.6-terra`; OpenAI and Copilot both report `gpt-5.1`. With
-- `models.id` as the primary key, those are the *same model* seen from two
-- harnesses, and the row's `provider` column cannot express that: it can only
-- name one owner, so whichever catalog synced last would win.
--
-- That was a live bug. `sync_discovered_models` upserts on `id` alone and
-- rewrote `provider` for non-manual rows, so running Codex discovery would
-- silently reassign 8 Copilot models to `codex` and move their `engine`. The
-- write path now refuses to touch a row owned by another provider (the overlap
-- is reported as `provider_conflicts` instead), which fixes the clobbering but
-- leaves the real information unrecorded: *Codex can also run these*.
--
-- What this migration adds
-- ------------------------
-- `model_engine_catalogs` — one row per (model, engine) pair, with:
--   * `source`        how the engine came to offer it: `catalog` (the engine
--                     enumerated it), `derived` (from the engine's declared
--                     provider support), or `manual`.
--   * `provenance`    free-form JSON for what the catalog said (hidden flag,
--                     default marker, API-facing model id, ...).
--
-- This is additive and safe to run against live data: it creates an empty table
-- and backfills nothing. Engine-model overlap is populated on the next discovery
-- run, and the derived rows are computed at read time, so an empty table simply
-- means "no catalog has been consulted yet".
--
-- `models.engine` is deliberately left alone. It is the model's *preferred
-- default engine*, not a capability claim — and that distinction is exactly what
-- this table makes representable.

CREATE TABLE IF NOT EXISTS model_engine_catalogs (
    model_id    VARCHAR(64) NOT NULL
                REFERENCES models(id) ON DELETE CASCADE,
    engine      VARCHAR(32) NOT NULL,
    source      VARCHAR(16) NOT NULL DEFAULT 'catalog',
    provenance  JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT model_engine_catalogs_pkey PRIMARY KEY (model_id, engine),
    CONSTRAINT ck_model_engine_catalogs_source
        CHECK (source IN ('catalog', 'derived', 'manual'))
);

-- "Which models does this engine offer?" — the query the engine-first UI makes.
CREATE INDEX IF NOT EXISTS idx_model_engine_catalogs_engine
    ON model_engine_catalogs (engine);

COMMENT ON TABLE model_engine_catalogs IS
    'Engines that offer a model. A model id is not provider-scoped, so the same '
    'row can be reported by several catalogs (Copilot and Codex both list '
    'gpt-5.6-terra). models.provider names the owning catalog; these rows record '
    'the overlap.';
