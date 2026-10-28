-- Migration 155: Record managed-tool runs in the usage ledger.
--
-- Background
-- ----------
-- Migration 154 recorded *sandbox* intervals. A managed-tool execution already
-- gets one: the executor creates a sandbox carrying ``source='managed_tool'``,
-- so its CPU, memory, and duration are captured today.
--
-- What is missing is the **run** itself. ``managed_tool_runs`` (087) holds
-- per-invocation detail — which tool, success or failure, ``duration_ms`` — but
-- it cascades on ``organizations``, so it disappears with the tenant exactly when
-- a final invoice still needs to be produced. A provider that bills per tool
-- *invocation* (rather than per CPU-second) therefore has no durable record.
--
-- This migration lets the same ledger hold tool-run rows.
--
-- Why not a second table
-- ----------------------
-- One ledger keeps a single answer to "what happened for this tenant in this
-- period". Two tables would force every consumer to union them, and the
-- idempotency, retention, and cross-tenant read logic would have to be written
-- twice and could drift.
--
-- The double-counting hazard, and how it is prevented
-- ---------------------------------------------------
-- This is the important part. A tool run and its sandbox describe the *same*
-- resources. Summing both would bill every custom-tool execution twice.
--
-- The rule is therefore absolute and enforced by the aggregation layer:
--
--   * ``resource_type = 'sandbox'``            → contributes to cpu/memory/duration
--   * ``resource_type = 'managed_tool_run'``   → contributes to invocation COUNTS only,
--                                                never to cpu/memory/duration
--
-- Tool-run rows carry no ``cpu_limit``/``memory_limit_mb`` for that reason: the
-- compute for a tool invocation lives on its sandbox row, and duplicating it here
-- would be an invitation to add it up twice. They record *duration* and *outcome*
-- (how long the invocation took, whether it succeeded), which is what a
-- per-invocation price and an availability report need.
--
-- Identity
-- --------
-- ``sandbox_id`` becomes nullable and ``managed_tool_run_id`` becomes the
-- idempotency key for tool rows, so a retried or repeated completion writes one
-- row. A CHECK enforces that a row names exactly one resource: a row with neither
-- is unidentifiable, and a row with both would be ambiguous about which key won a
-- conflict.
--
-- No foreign key on ``tool_id``, matching the rest of the ledger: a tool
-- definition can be deleted while its usage history must survive.

-- A tool row has no sandbox, and a sandbox row has no tool run.
ALTER TABLE resource_usage_events
    ALTER COLUMN sandbox_id DROP NOT NULL;

-- Which tool was invoked, so usage can be attributed to one definition. Plain
-- UUID for the same reason as the other attribution columns: cleanup of the
-- definition must not erase what it consumed.
ALTER TABLE resource_usage_events
    ADD COLUMN IF NOT EXISTS tool_id UUID;

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_resource_type_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_resource_type_check
    CHECK (resource_type IN ('sandbox', 'managed_tool_run'));

-- Exactly one identity per row. Neither would be unidentifiable; both would make
-- the ON CONFLICT target ambiguous.
ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_identity_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_identity_check
    CHECK (
        (resource_type = 'sandbox' AND sandbox_id IS NOT NULL)
        OR (
            resource_type = 'managed_tool_run'
            AND managed_tool_run_id IS NOT NULL
            AND sandbox_id IS NULL
        )
    );

-- A tool-run row must not claim compute, or a careless SUM would bill it twice
-- against its own sandbox row. Duration is allowed (it is the invocation's
-- wall-clock time), but the reservation columns are not.
ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_tool_run_no_reservation;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_tool_run_no_reservation
    CHECK (
        resource_type <> 'managed_tool_run'
        OR (cpu_limit IS NULL AND memory_limit_mb IS NULL)
    );

-- Idempotency key for tool rows. Partial so the many sandbox rows (which all have
-- a NULL managed_tool_run_id) do not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_resource_usage_events_tool_run
    ON resource_usage_events (managed_tool_run_id)
    WHERE managed_tool_run_id IS NOT NULL;

-- Per-tool reporting for a tenant over a period.
CREATE INDEX IF NOT EXISTS idx_resource_usage_events_org_tool
    ON resource_usage_events (organization_id, tool_id, started_at)
    WHERE tool_id IS NOT NULL;

COMMENT ON COLUMN resource_usage_events.resource_type IS
    'sandbox = a compute reservation (counts toward cpu/memory totals); managed_tool_run = one tool invocation (counts as an invocation only, never as compute).';
COMMENT ON COLUMN resource_usage_events.tool_id IS
    'Managed tool definition invoked by this row. Plain UUID: the definition may be deleted while its usage history must survive.';
