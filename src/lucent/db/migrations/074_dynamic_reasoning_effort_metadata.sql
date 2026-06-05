-- Migration 074: Refresh reasoning effort values from provider metadata
--
-- Earlier versions seeded model reasoning_efforts from Lucent heuristics. The
-- registry now treats these as provider-reported metadata only. This migration
-- removes stale guessed values and backfills exact values when provider catalog
-- metadata already contains them.

ALTER TABLE tasks
    ALTER COLUMN reasoning_effort TYPE VARCHAR(64);

ALTER TABLE schedules
    ALTER COLUMN reasoning_effort TYPE VARCHAR(64);

WITH normalized AS (
    SELECT
        id,
        CASE
            WHEN jsonb_typeof(discovery_metadata) = 'string'
             AND (discovery_metadata #>> '{}') ~ '^\s*[\[{]'
                THEN (discovery_metadata #>> '{}')::jsonb
            ELSE discovery_metadata
        END AS metadata
    FROM models
    WHERE discovery_source = 'provider'
      AND is_custom = false
), extracted AS (
    SELECT
        id,
        metadata,
        COALESCE(
            metadata -> 'supportedReasoningEfforts',
            metadata -> 'supported_reasoning_efforts',
            metadata -> 'reasoningEfforts',
            metadata -> 'reasoning_efforts',
            metadata -> 'reasoningEffortLevels',
            metadata -> 'reasoning_effort_levels',
            metadata -> 'supportedThinkingLevels',
            metadata -> 'supported_thinking_levels',
            metadata -> 'thinkingLevels',
            metadata -> 'thinking_levels',
            metadata -> 'supportedEfforts',
            metadata -> 'supported_efforts',
            metadata -> 'effortLevels',
            metadata -> 'effort_levels'
        ) AS levels
    FROM normalized
)
UPDATE models
SET
    discovery_metadata = extracted.metadata,
    reasoning_efforts = CASE
        WHEN jsonb_typeof(extracted.levels) = 'array'
            THEN ARRAY(
                SELECT effort
                FROM (
                    SELECT DISTINCT ON (lower(btrim(value)))
                        lower(btrim(value)) AS effort,
                        ord
                    FROM jsonb_array_elements_text(extracted.levels) WITH ORDINALITY AS t(value, ord)
                    WHERE btrim(value) <> ''
                    ORDER BY lower(btrim(value)), ord
                ) deduped
                ORDER BY ord
            )
        ELSE '{}'::text[]
    END,
    updated_at = NOW()
FROM extracted
WHERE models.id = extracted.id;
