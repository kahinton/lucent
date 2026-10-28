-- Migration 156: Allow tool-run outcome values in the usage ledger.
--
-- Migration 155 let the ledger hold ``managed_tool_run`` rows, but the
-- ``outcome`` vocabulary was still the sandbox one: ``stopped``, ``destroyed``,
-- ``failed``, ``expired``, ``unknown``. A managed-tool execution has its own
-- result vocabulary — ``completed`` and ``blocked`` — and neither was accepted.
--
-- The failure was silent and expensive: ``record_managed_tool_usage`` coerces an
-- unrecognised outcome to ``unknown`` to satisfy the old constraint, so a
-- *successful* tool run was stored as ``unknown`` and any per-invocation metering
-- that prices successes saw zero. Confirmed on the live instance: three
-- ``completed`` runs produced ``succeeded = 0``.
--
-- Coercing to ``unknown`` is the right defensive behaviour for an unexpected
-- value, but it must not be reachable for a value we know about. This widens the
-- constraint so the real vocabulary is storable, and keeps the coercion as a
-- last-resort guard rather than the normal path.
--
-- Sandbox outcomes are unchanged: a sandbox interval still ends as
-- stopped/destroyed/failed/expired, because that describes how a *reservation*
-- ended rather than how a *job* turned out.

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_outcome_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_outcome_check
    CHECK (
        outcome IN (
            'stopped', 'destroyed', 'failed', 'expired', 'unknown',
            'completed', 'blocked'
        )
    );

COMMENT ON COLUMN resource_usage_events.outcome IS
    'How the row ended. Sandboxes: stopped/destroyed/failed/expired. Tool runs: completed/failed/blocked.';

-- Correct any rows migrated from 155 that were coerced to "unknown" while the
-- wider vocabulary was unavailable. A tool row with a "failed" or "blocked"
-- outcome was already storable, so only "unknown" is ambiguous — and the run's
-- own record in managed_tool_runs is the authority when it still exists.
UPDATE resource_usage_events e
SET outcome = CASE
        WHEN r.status = 'completed' THEN 'completed'
        WHEN r.status = 'blocked' THEN 'blocked'
        ELSE 'failed'
    END
FROM managed_tool_runs r
WHERE e.resource_type = 'managed_tool_run'
  AND e.outcome = 'unknown'
  AND r.id = e.managed_tool_run_id;
