-- Migration 034: Add dependency_policy to requests
-- Controls whether failed/cancelled predecessor tasks block subsequent tasks.
-- 'strict' (default): only completed predecessors unblock the next step.
-- 'permissive': completed, failed, and cancelled all unblock.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS dependency_policy VARCHAR(16) NOT NULL DEFAULT 'strict';

ALTER TABLE requests
    ADD CONSTRAINT chk_requests_dependency_policy
    CHECK (dependency_policy IN ('strict', 'permissive'));
