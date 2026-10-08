-- Down migration for 133: remove explicit model classification.

DROP INDEX IF EXISTS idx_models_model_class;

ALTER TABLE models DROP COLUMN IF EXISTS model_class;