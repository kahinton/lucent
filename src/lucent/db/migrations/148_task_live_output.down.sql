-- Down migration for 148: drop the in-flight output scratch table.
--
-- Destructive but safe by construction: every row is transient scratch state
-- that the owning daemon deletes as soon as its task settles, and the durable
-- task result lives in `tasks.result`.

DROP TABLE IF EXISTS task_live_output;
