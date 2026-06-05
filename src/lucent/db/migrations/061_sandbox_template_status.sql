-- Migration 061: Sandbox template approval lifecycle
--
-- Adds an explicit approval status to sandbox templates so the cognitive
-- planner can only dispatch work to templates that have been vetted, and
-- so it can submit proposed templates that need human approval before use.
--
-- Existing templates are grandfathered as 'approved' — they were already
-- live in the system before this column existed.

ALTER TABLE sandbox_templates
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'approved',
    ADD COLUMN IF NOT EXISTS proposed_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS proposal_reason TEXT,
    ADD COLUMN IF NOT EXISTS reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

-- Status values: 'approved' (usable), 'proposed' (awaiting review), 'rejected'.
ALTER TABLE sandbox_templates
    DROP CONSTRAINT IF EXISTS ck_sandbox_tpl_status;
ALTER TABLE sandbox_templates
    ADD CONSTRAINT ck_sandbox_tpl_status
    CHECK (status IN ('approved', 'proposed', 'rejected'));

CREATE INDEX IF NOT EXISTS idx_sandbox_tpl_status
    ON sandbox_templates (organization_id, status);
