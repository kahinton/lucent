-- Migration 099: Index schedule-run lineage foreign keys
-- Deleting schedule runs updates these references through ON DELETE SET NULL.
-- Without indexes, large cleanup operations repeatedly scan both child tables.

CREATE INDEX IF NOT EXISTS idx_llm_sessions_schedule_run
    ON llm_sessions (schedule_run_id)
    WHERE schedule_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tool_call_audit_schedule_run
    ON tool_call_audit_log (schedule_run_id)
    WHERE schedule_run_id IS NOT NULL;