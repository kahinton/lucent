-- Migration 137: memory_access_log records reads only
--
-- A search result is not memory *use*. Logging 'search_result' wrote a row per
-- returned memory per search, and because the same statement also bumped
-- memories.last_accessed_at and flipped lifecycle_stage from
-- consolidating/archived to 'active', it inflated vitality scoring, the access
-- counts shown in the UI, and resurrected archived memories on every search.
-- Search logging has been removed from the code; this tightens the table so the
-- rows cannot come back.
--
-- NOTE: the constraint is validated against existing rows, so this fails if any
-- 'search_result' rows survive. Removing legacy rows is a separate ops step --
-- they were 99.8% of the table on installations that had them -- and is
-- deliberately not done here, because the migration runner applies each file in
-- a single startup transaction and deleting millions of rows would stall boot.

ALTER TABLE memory_access_log
    DROP CONSTRAINT IF EXISTS memory_access_log_access_type_check;

ALTER TABLE memory_access_log
    ADD CONSTRAINT memory_access_log_access_type_check
    CHECK (access_type = 'view');
