-- Down migration for 158: restore the python-only runtime constraint.
--
-- Narrowing the CHECK requires every row to be `python` again. Deleting the
-- non-python tool definitions would destroy user-authored content and any
-- consent rows pointing at it, so instead the definitions are archived: they
-- remain readable for audit, are no longer active, and therefore cannot be
-- invoked by an agent on downgraded binaries.

UPDATE managed_tool_definitions
SET status = 'archived', updated_at = NOW()
WHERE runtime_type <> 'python';

ALTER TABLE managed_tool_definitions
    DROP CONSTRAINT IF EXISTS managed_tool_definitions_runtime_type_check;

ALTER TABLE managed_tool_definitions
    ADD CONSTRAINT managed_tool_definitions_runtime_type_check
    CHECK (runtime_type = 'python');
