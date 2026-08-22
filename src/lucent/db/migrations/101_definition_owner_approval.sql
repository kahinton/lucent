-- Migration 101: Staged owner approval for hooks and managed tools
--
-- Hooks and managed tools may require an owner acknowledgement before an
-- organization administrator performs the final safety approval.

ALTER TABLE hook_definitions
    DROP CONSTRAINT IF EXISTS hook_definitions_status_check;

ALTER TABLE hook_definitions
    ADD CONSTRAINT hook_definitions_status_check
    CHECK (status IN ('proposed', 'owner_approved', 'active', 'rejected'));

ALTER TABLE hook_definitions
    ADD COLUMN IF NOT EXISTS owner_approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_approved_at TIMESTAMPTZ;

ALTER TABLE managed_tool_definitions
    DROP CONSTRAINT IF EXISTS managed_tool_definitions_status_check;

ALTER TABLE managed_tool_definitions
    ADD CONSTRAINT managed_tool_definitions_status_check
    CHECK (status IN ('proposed', 'owner_approved', 'active', 'rejected', 'archived'));

ALTER TABLE managed_tool_definitions
    ADD COLUMN IF NOT EXISTS owner_approved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS owner_approved_at TIMESTAMPTZ;

COMMENT ON COLUMN hook_definitions.owner_approved_by IS
  'Owner or group administrator who approved this hook before final administrative approval.';
COMMENT ON COLUMN hook_definitions.owner_approved_at IS
  'When the owner approved this hook before final administrative approval.';
COMMENT ON COLUMN managed_tool_definitions.owner_approved_by IS
  'Owner or group administrator who approved this tool before final administrative approval.';
COMMENT ON COLUMN managed_tool_definitions.owner_approved_at IS
  'When the owner approved this tool before final administrative approval.';