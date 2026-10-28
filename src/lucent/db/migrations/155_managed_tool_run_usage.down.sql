-- Down migration for 155: drop managed-tool run rows from the usage ledger.
--
-- Removes only the tool-run rows and the columns introduced by 155. Sandbox
-- intervals from migration 154 are kept, so compute history is unaffected.

DROP INDEX IF EXISTS idx_resource_usage_events_org_tool;
DROP INDEX IF EXISTS uniq_resource_usage_events_tool_run;

DELETE FROM resource_usage_events WHERE resource_type = 'managed_tool_run';

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_tool_run_no_reservation;
ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_identity_check;

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_resource_type_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_resource_type_check
    CHECK (resource_type IN ('sandbox'));

ALTER TABLE resource_usage_events
    DROP COLUMN IF EXISTS tool_id;

ALTER TABLE resource_usage_events
    ALTER COLUMN sandbox_id SET NOT NULL;
