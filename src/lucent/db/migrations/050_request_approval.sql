-- 050: Request approval gate
-- Adds pre-work approval fields to requests.
-- Daemon-created requests can require human approval before work begins.

-- Approval status tracks the approval lifecycle independently of work status.
-- Values: auto_approved, pending_approval, approved, rejected
ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS approval_status VARCHAR(16) NOT NULL DEFAULT 'auto_approved',
    ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS approval_comment TEXT;

-- Index for finding requests that need approval
CREATE INDEX IF NOT EXISTS idx_requests_approval_status
    ON requests (approval_status) WHERE approval_status = 'pending_approval';

-- Backfill: all existing requests are auto_approved (they already ran)
UPDATE requests SET approval_status = 'auto_approved' WHERE approval_status = 'auto_approved';
