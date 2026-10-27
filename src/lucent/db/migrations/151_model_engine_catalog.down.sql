-- Down migration for 151: drop the engine-catalog overlap table.

DROP INDEX IF EXISTS idx_model_engine_catalogs_engine;
DROP TABLE IF EXISTS model_engine_catalogs;
