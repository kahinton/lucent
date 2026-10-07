-- Rollback migration 121.
--
-- lucent: warning=drops auth IDs and clearances

DROP TABLE IF EXISTS auth_clearances;
DROP TABLE IF EXISTS auth_ids;
