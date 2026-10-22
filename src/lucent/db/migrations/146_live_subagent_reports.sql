-- Migration 146: Push subagent report cards to the browser immediately.
--
-- Migration 145 signals the report *service*; this signals the *browser*. The
-- report service writes a visible ``subagent_report`` event into the session
-- transcript, and the chat page should show it without waiting for its poll.
--
-- Scoped deliberately narrowly:
--   * only llm_session_events inserts,
--   * only event_type = 'subagent_report',
-- so ordinary tool/message events (which are numerous) never generate a
-- websocket-equivalent signal. The payload stays content-free and matches the
-- existing lucent_live_update contract: the browser re-reads through its
-- authenticated routes.

CREATE OR REPLACE FUNCTION notify_live_subagent_report()
RETURNS TRIGGER AS $$
DECLARE
    target_org_id UUID;
    target_user_id UUID;
BEGIN
    SELECT organization_id, user_id
    INTO target_org_id, target_user_id
    FROM llm_sessions
    WHERE id = NEW.session_id;

    IF target_org_id IS NOT NULL THEN
        PERFORM pg_notify(
            'lucent_live_update',
            jsonb_build_object(
                'organization_id', target_org_id,
                'user_id', target_user_id,
                'kind', 'llm_session_events',
                'session_id', NEW.session_id
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS live_web_subagent_reports_notify ON llm_session_events;
CREATE TRIGGER live_web_subagent_reports_notify
    AFTER INSERT ON llm_session_events
    FOR EACH ROW
    WHEN (NEW.event_type = 'subagent_report')
    EXECUTE FUNCTION notify_live_subagent_report();
