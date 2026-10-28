-- Migration 154: Immutable resource-usage ledger.
--
-- Background
-- ----------
-- Sandbox resource consumption is *observable* today but not *durable*:
--
--   * `sandboxes` (021) stores the requested limits inside its `config` JSONB
--     and carries `ready_at` / `stopped_at` / `destroyed_at`, so CPU count,
--     memory, and a derivable duration are all technically present.
--   * `managed_tool_runs` (087) records `duration_ms` for sandbox-backed tool
--     executions.
--
-- Three properties of those tables make them unusable as a usage record:
--
--   1. **They are deleted with the tenant.** `OrganizationRepository.delete`
--      clears `sandboxes` before removing the organization, and
--      `managed_tool_runs` cascades on `organizations`. Usage therefore
--      disappears exactly when a final invoice still needs to be produced.
--   2. **Duration is inferred, not recorded.** A crash between start and
--      teardown leaves no end timestamp at all, so "how long did this run" is
--      unanswerable for precisely the cases an operator would dispute.
--   3. **They are mutable operational rows.** `sandboxes.status` is overwritten
--      across the lifecycle; a usage figure must not move because an
--      operational record was edited or reaped.
--
-- This migration adds an append-only ledger of *closed* usage intervals.
--
-- Why a ledger and not columns on `sandboxes`
-- -------------------------------------------
-- A ledger row is written once, when the resource stops consuming, and is never
-- updated for operational reasons. That keeps the usage figure stable, makes
-- re-computation idempotent (the unique key below), and — most importantly —
-- lets the row outlive the sandbox row and even the organization.
--
-- `organization_id` is therefore `ON DELETE SET NULL`, deliberately unlike
-- `llm_token_usage` (108) and `managed_tool_runs` (087), which cascade. Usage
-- must be able to close a final period for a tenant that has been offboarded.
-- `organization_name` is snapshotted at write time so a row remains attributable
-- after the FK is nulled; that is the one piece of tenant identity this table
-- keeps on purpose, and it is the name the customer chose, not personal data.
-- An operator who must not retain it can delete the row explicitly.
--
-- `sandbox_id` is `TEXT` with no foreign key, for the same reason: the sandbox
-- row is deleted by design and a usage row must not vanish with it. The
-- unique constraint makes the ledger idempotent — re-closing the same sandbox
-- (a stop followed by a destroy, or a retried teardown) updates the existing
-- interval instead of double-counting it.
--
-- `task_id` / `request_id` / `managed_tool_run_id` are likewise plain UUIDs with
-- no FK, so attribution survives cleanup of the things that caused the usage.
--
-- What a row means
-- ----------------
-- One row = one sandbox's resource reservation over one interval, measured in
-- wall-clock seconds with the CPU and memory limits it held. Chargeable totals
-- are derived by the aggregation layer, not stored: `cpu_limit * duration_seconds`
-- is CPU-seconds and `memory_limit_mb * duration_seconds` is MB-seconds. Storing
-- derived totals would bake a pricing model into the schema; keeping the
-- primitives lets an outside metering system apply whatever rating it wants.
--
-- Limits are the *requested* limits from the sandbox config. The Docker backend
-- may cap a request to the host's advertised capacity, so actual consumption can
-- be lower than recorded — never higher. That direction is the safe one for
-- invoicing: the ledger cannot overstate what a tenant was given.
--
-- Rollout safety
-- --------------
-- Additive only. The table starts empty and nothing reads it yet, so this
-- migration changes no behaviour. Historical usage is backfilled by
-- `lucent.services.usage.backfill_from_sandboxes`, which is explicit and
-- idempotent rather than being run inside this migration.

CREATE TABLE IF NOT EXISTS resource_usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Nullable on purpose: see the header note about offboarded tenants.
    organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    organization_name TEXT NOT NULL,

    resource_type VARCHAR(32) NOT NULL DEFAULT 'sandbox',
    -- What caused the resource to exist. `task` and `managed_tool` are metered
    -- workloads; `interactive` is a member driving the sandbox UI by hand.
    source VARCHAR(32) NOT NULL DEFAULT 'unknown',

    -- No FKs: every one of these rows is deleted by ordinary cleanup, and the
    -- ledger must survive that.
    sandbox_id TEXT NOT NULL,
    task_id UUID,
    request_id UUID,
    managed_tool_run_id UUID,

    -- Requested reservation. NULL means "not recorded", which is distinct from
    -- zero (a genuinely zero limit, e.g. a suspended resource).
    cpu_limit NUMERIC(8, 3),
    memory_limit_mb BIGINT,
    disk_limit TEXT,

    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,
    duration_seconds BIGINT NOT NULL,

    -- How the interval ended. `expired` is the auto-destroy timeout, which is
    -- distinguishable from an operator teardown for cost-dispute purposes.
    outcome VARCHAR(16) NOT NULL DEFAULT 'unknown',

    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_interval_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_interval_check
    CHECK (ended_at >= started_at AND duration_seconds >= 0);

-- Negative consumption is meaningless. Zero is legal (a sandbox that never left
-- `creating` bills nothing) so the columns stay honest rather than NULL.
ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_limits_non_negative;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_limits_non_negative
    CHECK (
        (cpu_limit IS NULL OR cpu_limit >= 0)
        AND (memory_limit_mb IS NULL OR memory_limit_mb >= 0)
    );

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_resource_type_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_resource_type_check
    CHECK (resource_type IN ('sandbox'));

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_source_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_source_check
    CHECK (source IN ('task', 'managed_tool', 'interactive', 'unknown'));

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_outcome_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_outcome_check
    CHECK (outcome IN ('stopped', 'destroyed', 'failed', 'expired', 'unknown'));

-- Idempotency key. Teardown can be reached more than once (stop then destroy, a
-- retried destroy, a backfill after a crash), and each of those must close the
-- same interval rather than bill it twice.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_resource_usage_events_sandbox
    ON resource_usage_events (sandbox_id);

-- Usage reads are "this tenant, this period" and "this period, everyone".
CREATE INDEX IF NOT EXISTS idx_resource_usage_events_org_started
    ON resource_usage_events (organization_id, started_at);
CREATE INDEX IF NOT EXISTS idx_resource_usage_events_started
    ON resource_usage_events (started_at);

COMMENT ON TABLE resource_usage_events IS
    'Append-only ledger of closed sandbox usage intervals. Survives tenant deletion so a final usage period can still be closed.';
COMMENT ON COLUMN resource_usage_events.organization_name IS
    'Snapshot of the tenant name at write time; retained after organization_id is nulled by tenant deletion.';
COMMENT ON COLUMN resource_usage_events.cpu_limit IS
    'Requested CPU limit held for the interval. Backends may cap to host capacity, so actual usage is <= this value.';
COMMENT ON COLUMN resource_usage_events.duration_seconds IS
    'Wall-clock seconds the reservation was held. Chargeable totals are derived (cpu_limit * duration_seconds), never stored.';
