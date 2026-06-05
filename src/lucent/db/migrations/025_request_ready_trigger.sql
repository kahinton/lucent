-- Migration 025: Event-driven cognitive wake on new requests via PG NOTIFY
--
-- Adds a trigger that fires pg_notify('request_ready', ...) when a new request
-- is inserted with source = 'user' or 'api'. This allows the daemon's cognitive
-- loop to wake immediately instead of waiting for the next scheduled cycle.
--
-- Background requests (source = 'cognitive', 'schedule', 'daemon') still wait
-- for the normal cycle interval — they're not urgent.
--
-- ═══════════════════════════════════════════════════════════════════════════
-- WAKE MATRIX — which transitions trigger pg_notify vs rely on polling
-- ═══════════════════════════════════════════════════════════════════════════
--
--  Transition                      | Wake mechanism            | Channel
--  ────────────────────────────────┼───────────────────────────┼──────────────────
--  Request created (user/api)      | DB trigger (this file)    | request_ready
--  Request created (cognitive/     | Poll only                 | —
--    schedule/daemon)              |                           |
--  Task status → ready             | DB trigger (mig 018)      | task_ready
--  Feedback: approve               | App code (daemon.py)      | request_ready
--  Feedback: reject                | App code (daemon.py)      | request_ready
--  Feedback: comment               | Poll only                 | —
--  Feedback: reset                 | Poll only                 | —
--
-- Notify failures in app code are retried once then logged at ERROR level.
-- The daemon's poll cycle (default 15 min) serves as the fallback for all
-- transitions — notify is an optimization, not a correctness requirement.
-- ═══════════════════════════════════════════════════════════════════════════

-- Trigger function: emit a JSON payload on the 'request_ready' channel.
CREATE OR REPLACE FUNCTION notify_request_ready()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'request_ready',
        jsonb_build_object(
            'request_id', NEW.id,
            'organization_id', NEW.organization_id,
            'source', NEW.source,
            'priority', NEW.priority
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- New user/api request → wake cognitive loop immediately
DROP TRIGGER IF EXISTS request_created_notify ON requests;
CREATE TRIGGER request_created_notify
    AFTER INSERT ON requests
    FOR EACH ROW
    WHEN (NEW.source IN ('user', 'api'))
    EXECUTE FUNCTION notify_request_ready();
