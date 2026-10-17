-- Down migration for 137: allow 'search_result' rows again.
--
-- Rolling back re-opens the table to search-result logging. The code no longer
-- writes them, so this only matters if older code is restored alongside it.

ALTER TABLE memory_access_log
    DROP CONSTRAINT IF EXISTS memory_access_log_access_type_check;

ALTER TABLE memory_access_log
    ADD CONSTRAINT memory_access_log_access_type_check
    CHECK (access_type = ANY (ARRAY['view'::text, 'search_result'::text]));
