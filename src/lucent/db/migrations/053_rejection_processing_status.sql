-- Add rejection_processing status for the rejection feedback loop.
-- Rejected requests enter this state so the daemon can process the
-- rejection feedback before the request is fully cancelled.

-- Widen status column to accommodate 'rejection_processing' (21 chars)
ALTER TABLE requests ALTER COLUMN status TYPE varchar(24);

-- Update the check constraint to include new status
ALTER TABLE requests DROP CONSTRAINT IF EXISTS chk_requests_status;
ALTER TABLE requests ADD CONSTRAINT chk_requests_status
    CHECK (status IN (
        'pending', 'planned', 'in_progress', 'review',
        'needs_rework', 'completed', 'failed', 'cancelled',
        'rejection_processing'
    ));
