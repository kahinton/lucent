-- Down migration for 168: drop the trigger payload column.
--
-- Rows that carry a payload lose it; every other row is unaffected. The
-- description-based trigger rendering (the pre-168 behaviour) is untouched,
-- so webhook workflows keep firing exactly as they did before.
--
-- No explicit BEGIN/COMMIT: the runner opens one transaction per file.

ALTER TABLE requests
    DROP COLUMN IF EXISTS trigger_payload;
