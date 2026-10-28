-- Down migration for 154: remove the resource-usage ledger.
--
-- Drops only the ledger. `sandboxes` and `managed_tool_runs` are untouched, so
-- operational sandbox state is unaffected; only the usage history is lost.

DROP INDEX IF EXISTS idx_resource_usage_events_started;
DROP INDEX IF EXISTS idx_resource_usage_events_org_started;
DROP TABLE IF EXISTS resource_usage_events;
