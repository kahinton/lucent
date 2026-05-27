-- Migration 045: Structured task output contracts
-- Adds output contract declaration and structured result storage to tasks.
-- Fully additive — no existing data or behavior changes.

ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS output_contract JSONB,
  ADD COLUMN IF NOT EXISTS result_structured JSONB,
  ADD COLUMN IF NOT EXISTS result_summary TEXT,
  ADD COLUMN IF NOT EXISTS validation_status VARCHAR(24) NOT NULL DEFAULT 'not_applicable',
  ADD COLUMN IF NOT EXISTS validation_errors JSONB;

-- Validation status values:
--   'not_applicable'    — no contract declared (legacy tasks)
--   'valid'             — structured output extracted and validated
--   'invalid'           — validation failed, task was failed
--   'extraction_failed' — no <task_output> block found
--   'fallback_used'     — validation failed, continued with text-only
--   'repair_succeeded'  — validation failed, repair pass succeeded

COMMENT ON COLUMN tasks.output_contract IS
  'JSON Schema contract for expected structured output. Contains: json_schema (the schema), on_failure (fail|fallback|retry_then_fallback), max_retries (int, default 1)';
COMMENT ON COLUMN tasks.result_structured IS
  'Validated structured output extracted from agent response, stored as JSONB';
COMMENT ON COLUMN tasks.result_summary IS
  'Brief text summary of task result for prompt-efficient context passing';
COMMENT ON COLUMN tasks.validation_status IS
  'Output validation result: not_applicable, valid, invalid, extraction_failed, fallback_used, repair_succeeded';
COMMENT ON COLUMN tasks.validation_errors IS
  'JSON array of validation error messages when validation_status is not valid';
