-- Migration: Add engine override to model definitions
-- NULL means auto-detect (current behavior via provider-based routing in get_engine_for_model()).
-- Valid explicit values: 'copilot', 'langchain'

ALTER TABLE models ADD COLUMN IF NOT EXISTS engine VARCHAR DEFAULT NULL;
