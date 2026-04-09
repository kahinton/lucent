-- System schedules: built-in schedules that ship with every deployment.
-- They can be modified (e.g., change interval, disable) but not deleted.

ALTER TABLE schedules ADD COLUMN IF NOT EXISTS is_system BOOLEAN DEFAULT false;

-- Index for fast system schedule lookups during seeding
CREATE INDEX IF NOT EXISTS idx_schedules_system ON schedules(is_system) WHERE is_system = true;
