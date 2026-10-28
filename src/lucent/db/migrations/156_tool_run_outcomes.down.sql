-- Down migration for 156: restore the sandbox-only outcome vocabulary.
--
-- Tool-run outcomes are folded back into values the older constraint accepts so
-- the narrower CHECK can be re-applied without deleting history.

UPDATE resource_usage_events
SET outcome = CASE
        WHEN outcome = 'completed' THEN 'destroyed'
        WHEN outcome = 'blocked' THEN 'failed'
        ELSE outcome
    END
WHERE outcome IN ('completed', 'blocked');

ALTER TABLE resource_usage_events
    DROP CONSTRAINT IF EXISTS resource_usage_events_outcome_check;
ALTER TABLE resource_usage_events
    ADD CONSTRAINT resource_usage_events_outcome_check
    CHECK (
        outcome IN ('stopped', 'destroyed', 'failed', 'expired', 'unknown')
    );
