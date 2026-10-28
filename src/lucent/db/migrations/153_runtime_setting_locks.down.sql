-- Down migration for 153: remove instance-operator setting locks.
--
-- Drops the lock table only. `runtime_settings` is untouched, so every tenant
-- keeps the values it had and simply regains the ability to edit them.

DROP INDEX IF EXISTS idx_runtime_setting_locks_org;
DROP TABLE IF EXISTS runtime_setting_locks;
