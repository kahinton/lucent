-- Migration 048: Migrate review-related memories into the reviews table
--
-- This is the core motivation for the reviews refactoring: removing review
-- pollution from the memory store. Reviews stored as memories (tagged with
-- 'needs-review', 'feedback-approved', 'feedback-rejected') are migrated
-- into the first-class reviews table and the source memories are soft-deleted.
--
-- Strategy:
--   1. Identify memories with review feedback tags ('feedback-approved',
--      'feedback-rejected') that represent completed review decisions.
--   2. For each, try to find a matching request (via metadata or content).
--   3. Insert a review row for each migrated memory.
--   4. Re-tag source memories as 'review-migrated' and soft-delete them
--      so they no longer pollute the memory store but remain recoverable.
--
-- Reversible: DOWN section restores soft-deleted memories and drops migrated reviews.

-- ── UP ──────────────────────────────────────────────────────────────────

-- Step 1: Migrate 'feedback-approved' memories into reviews table.
-- These are memories where a human approved daemon work via the web UI.
-- The memory's metadata->feedback->reviewed_by provides reviewer context.
-- We link to requests by checking if the memory references a request in metadata.
INSERT INTO reviews (request_id, organization_id, reviewer_display_name,
                     status, comments, source, created_at)
SELECT
    r.id AS request_id,
    m.organization_id,
    COALESCE(m.metadata->'feedback'->>'reviewed_by', 'migrated-user') AS reviewer_display_name,
    'approved' AS status,
    COALESCE(m.metadata->'feedback'->>'comment', 'Migrated from memory-based review') AS comments,
    'human' AS source,
    m.updated_at AS created_at
FROM memories m
-- Join memories to requests: try matching on metadata.related_entities or content references
CROSS JOIN LATERAL (
    SELECT req.id
    FROM requests req
    WHERE req.organization_id = m.organization_id
      -- Match by request title appearing in memory content
      AND (m.content ILIKE '%' || req.title || '%'
           OR m.metadata::text ILIKE '%' || CAST(req.id AS text) || '%')
    ORDER BY req.created_at DESC
    LIMIT 1
) r
WHERE m.deleted_at IS NULL
  AND 'feedback-approved' = ANY(m.tags)
  AND 'daemon' = ANY(m.tags)
  AND NOT EXISTS (
      -- Don't re-migrate if already processed
      SELECT 1 FROM reviews rv
      WHERE rv.request_id = r.id
        AND rv.organization_id = m.organization_id
        AND rv.comments = COALESCE(m.metadata->'feedback'->>'comment', 'Migrated from memory-based review')
        AND rv.source = 'human'
  );

-- Step 2: Migrate 'feedback-rejected' memories into reviews table.
INSERT INTO reviews (request_id, organization_id, reviewer_display_name,
                     status, comments, source, created_at)
SELECT
    r.id AS request_id,
    m.organization_id,
    COALESCE(m.metadata->'feedback'->>'reviewed_by', 'migrated-user') AS reviewer_display_name,
    'rejected' AS status,
    COALESCE(m.metadata->'feedback'->>'comment', 'Migrated from memory-based review (rejected)') AS comments,
    'human' AS source,
    m.updated_at AS created_at
FROM memories m
CROSS JOIN LATERAL (
    SELECT req.id
    FROM requests req
    WHERE req.organization_id = m.organization_id
      AND (m.content ILIKE '%' || req.title || '%'
           OR m.metadata::text ILIKE '%' || CAST(req.id AS text) || '%')
    ORDER BY req.created_at DESC
    LIMIT 1
) r
WHERE m.deleted_at IS NULL
  AND 'feedback-rejected' = ANY(m.tags)
  AND 'daemon' = ANY(m.tags)
  AND NOT EXISTS (
      SELECT 1 FROM reviews rv
      WHERE rv.request_id = r.id
        AND rv.organization_id = m.organization_id
        AND rv.comments = COALESCE(m.metadata->'feedback'->>'comment', 'Migrated from memory-based review (rejected)')
        AND rv.source = 'human'
  );

-- Step 3: Soft-delete and re-tag the migrated memories.
-- This removes review pollution from the memory store while keeping
-- the data recoverable via the deleted_at column.
UPDATE memories
SET deleted_at = NOW(),
    tags = array_remove(array_remove(array_remove(
        array_append(tags, 'review-migrated'),
        'needs-review'), 'feedback-approved'), 'feedback-rejected')
WHERE deleted_at IS NULL
  AND 'daemon' = ANY(tags)
  AND ('feedback-approved' = ANY(tags) OR 'feedback-rejected' = ANY(tags));

-- Step 4: Also soft-delete lingering 'needs-review' memories that have
-- no feedback yet (orphaned review requests). These are stale daemon
-- outputs that were never reviewed.
UPDATE memories
SET deleted_at = NOW(),
    tags = array_remove(array_append(tags, 'review-migrated'), 'needs-review')
WHERE deleted_at IS NULL
  AND 'daemon' = ANY(tags)
  AND 'needs-review' = ANY(tags)
  AND NOT ('feedback-approved' = ANY(tags))
  AND NOT ('feedback-rejected' = ANY(tags))
  -- Only soft-delete if older than 7 days (don't touch recent active reviews)
  AND created_at < NOW() - INTERVAL '7 days';


-- ── DOWN (rollback) ─────────────────────────────────────────────────────
-- To reverse this migration:
--
-- 1. Restore soft-deleted review memories:
--    UPDATE memories
--    SET deleted_at = NULL,
--        tags = array_remove(tags, 'review-migrated')
--    WHERE 'review-migrated' = ANY(tags);
--
-- 2. Delete migrated review records (those with 'Migrated from memory-based review' comments):
--    DELETE FROM reviews
--    WHERE comments LIKE 'Migrated from memory-based review%';
