-- Migration 091: Performance fixes for tag-only search of `memories`.
--
-- Context (Pattern 2, search_memories MCP timeouts 2026-05-30 → 2026-06-11):
-- Tag-only calls such as `search_memories(tags=["validated"], limit=10)`
-- repeatedly hit the MCP timeout (-32001). The `tags` column is already
-- covered by the GIN index `idx_memories_tags` (migration 001), but the
-- planner often prefers the partial btree access-control indexes
-- (e.g. `idx_memories_org_id`) and then `Filter: (tags @> ...)` inline
-- on every candidate row. On large tenants this scans tens of thousands
-- of rows for a 10-row result and blows the MCP request budget.
--
-- Two complementary fixes here (paired with the SQL change in
-- src/lucent/db/memory.py which adds an explicit `::text[]` cast and uses
-- a `WITH ... AS MATERIALIZED` CTE when tags are the primary selector):
--
--   1. Add a *partial* GIN index restricted to live rows. It's substantially
--      smaller than the unconditional GIN index and lets the planner pick
--      a tag-first plan more aggressively because its estimated cost drops.
--
--   2. Raise the per-column statistics target on `tags` so that
--      ndistinct / MCV estimates for `tags @> ARRAY[...]` are accurate
--      enough that the planner stops underestimating selectivity for
--      moderately-common tags like 'validated'.
--
-- Notes:
-- * `CREATE INDEX CONCURRENTLY` cannot run inside the transactional migration
--   runner (src/lucent/db/pool.py wraps each file in `conn.transaction()`),
--   so this uses a plain `CREATE INDEX IF NOT EXISTS`. The memories table is
--   small enough that the brief AccessExclusiveLock on index create is
--   acceptable; operators who want to deploy this online can apply the
--   equivalent `CONCURRENTLY` form manually and the `IF NOT EXISTS` guard
--   here will then be a no-op.
-- * `text[]` is the actual column type (confirmed via `\d memories`), so
--   the default `_text_ops` opclass is the right choice — same as the
--   existing `idx_memories_tags`.

CREATE INDEX IF NOT EXISTS idx_memories_tags_active
    ON memories USING GIN (tags)
    WHERE deleted_at IS NULL;

COMMENT ON INDEX idx_memories_tags_active IS
    'Partial GIN index on tags restricted to live rows; supports the '
    'tag-only search_memories fast path added in src/lucent/db/memory.py.';

ALTER TABLE memories ALTER COLUMN tags SET STATISTICS 1000;

ANALYZE memories;
