-- Rollback migration 065_memory_shadow_scores.sql

DROP INDEX IF EXISTS ix_msv_divergence;
DROP INDEX IF EXISTS ix_msv_strategy_computed;
DROP TABLE IF EXISTS memory_shadow_scores;
