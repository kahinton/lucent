-- Migration 145: Notify the originating conversation when delegated work reports.
--
-- A chat session that hands work to a request becomes a sub-agent supervisor:
-- the user expects to be told when a task finishes and when the request as a
-- whole reaches a decision point. Nothing pushed that before — the browser only
-- discovered results by polling, and the conversation model had no idea its
-- delegated work had advanced.
--
-- This trigger publishes a *content-free* signal on 'lucent_subagent_report':
--   {session_id, request_id, task_id?, kind, organization_id, user_id}
-- The consumer re-reads the real state (task result, request status) before
-- building anything user-visible, so the payload can never leak work content
-- into a notification and can never go stale.
--
-- Only milestone transitions are reported: a task reaching a terminal state, or
-- a request reaching a state a person must act on or be told about. Intermediate
-- per-step progress stays in the task timeline where it belongs.
--
-- One session may be linked to a request through two paths, both of which are
-- resolved here so a session never misses its own work:
--   1. requests.origin_session_id — set by create_request from trusted headers
--   2. llm_session_requests (relation='created') — the explicit lineage edge
--
-- Reportable task statuses are excluded when they belong to the post-completion
-- review task: that is bookkeeping, not delivered work.

CREATE OR REPLACE FUNCTION notify_subagent_report()
RETURNS TRIGGER AS $$
DECLARE
    target_request_id UUID;
    target_task_id UUID;
    target_org_id UUID;
    report_kind TEXT;
    origin_user_id UUID;
    session_row RECORD;
BEGIN
    IF TG_TABLE_NAME = 'tasks' THEN
        target_request_id := NEW.request_id;
        target_task_id := NEW.id;
        target_org_id := NEW.organization_id;
        report_kind := 'task.' || NEW.status;
        -- The internal review task is a lifecycle step, not delivered work.
        IF NEW.title = 'Post-completion review' THEN
            RETURN NEW;
        END IF;
    ELSE
        target_request_id := NEW.id;
        target_task_id := NULL;
        target_org_id := NEW.organization_id;
        report_kind := 'request.' || NEW.status;
    END IF;

    SELECT created_by INTO origin_user_id FROM requests WHERE id = target_request_id;

    FOR session_row IN
        SELECT DISTINCT s.id AS session_id
        FROM llm_sessions s
        WHERE s.organization_id = target_org_id
          AND (
              s.id = (SELECT origin_session_id FROM requests WHERE id = target_request_id)
              OR s.id IN (
                  SELECT lsr.session_id
                  FROM llm_session_requests lsr
                  WHERE lsr.request_id = target_request_id
                    AND lsr.relation = 'created'
              )
          )
          AND s.status NOT IN ('archived', 'deleted')
    LOOP
        PERFORM pg_notify(
            'lucent_subagent_report',
            jsonb_build_object(
                'session_id', session_row.session_id,
                'request_id', target_request_id,
                'task_id', target_task_id,
                'kind', report_kind,
                'organization_id', target_org_id,
                'user_id', origin_user_id
            )::text
        );
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Task result: a claimed/running task settled into a reportable outcome.
DROP TRIGGER IF EXISTS subagent_report_task_notify ON tasks;
CREATE TRIGGER subagent_report_task_notify
    AFTER UPDATE OF status ON tasks
    FOR EACH ROW
    WHEN (
        OLD.status IS DISTINCT FROM NEW.status
        AND NEW.status IN ('completed', 'failed', 'cancelled', 'needs_review')
        AND OLD.status IN ('claimed', 'running')
    )
    EXECUTE FUNCTION notify_subagent_report();

-- Request result: a state a person must act on or be told about.
-- 'planned' is deliberately absent — nothing in the system writes it.
DROP TRIGGER IF EXISTS subagent_report_request_notify ON requests;
CREATE TRIGGER subagent_report_request_notify
    AFTER UPDATE OF status ON requests
    FOR EACH ROW
    WHEN (
        OLD.status IS DISTINCT FROM NEW.status
        AND NEW.status IN ('review', 'needs_rework', 'completed', 'failed', 'cancelled')
    )
    EXECUTE FUNCTION notify_subagent_report();
