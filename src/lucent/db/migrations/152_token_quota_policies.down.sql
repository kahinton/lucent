-- Down migration for 152: remove token quota configuration.
--
-- Drops only the configuration tables added by 152. The ledger index this
-- migration added is also removed because it exists solely to serve quota
-- aggregation; `llm_token_usage` itself and every row in it are untouched.

DROP INDEX IF EXISTS idx_llm_token_usage_org_model_created;

DROP TABLE IF EXISTS token_quota_assignments;
DROP TABLE IF EXISTS token_quota_policies;
