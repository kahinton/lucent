-- Migration 033: Add CHECK constraint on requests.source
-- Enforces canonical source values at the database level.

-- First update any non-canonical values that may already exist.
UPDATE requests SET source = 'api' WHERE source NOT IN ('user', 'cognitive', 'api', 'daemon', 'schedule');

ALTER TABLE requests
    ADD CONSTRAINT chk_requests_source
    CHECK (source IN ('user', 'cognitive', 'api', 'daemon', 'schedule'));
