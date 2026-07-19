-- Migration 092: Notify the daemon for every newly inserted request.
--
-- The request_ready listener now drives focused, immediate task decomposition.
-- Every request source therefore needs the same wake behavior. The daemon keeps
-- a periodic zero-task backfill as a fallback for missed notifications, while
-- request task-existence checks and advisory locks make duplicate notifications
-- safe across multiple daemon instances.
--
-- Rollback:
--   DROP TRIGGER IF EXISTS request_created_notify ON requests;
--   CREATE TRIGGER request_created_notify
--       AFTER INSERT ON requests
--       FOR EACH ROW
--       WHEN (NEW.source IN ('user', 'api'))
--       EXECUTE FUNCTION notify_request_ready();

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

DROP TRIGGER IF EXISTS request_created_notify ON requests;
CREATE TRIGGER request_created_notify
    AFTER INSERT ON requests
    FOR EACH ROW
    EXECUTE FUNCTION notify_request_ready();
