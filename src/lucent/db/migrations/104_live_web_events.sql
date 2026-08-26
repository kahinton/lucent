-- Migration 104: PostgreSQL refresh signals for the live web UI.
--
-- Signals are deliberately compact and contain no request, task, file, or
-- handoff content. The receiving browser always re-fetches through its normal
-- authenticated routes. LISTEN/NOTIFY is transient, so the UI retains polling
-- as a recovery mechanism.

CREATE OR REPLACE FUNCTION notify_live_web_event()
RETURNS TRIGGER AS $$
DECLARE
    target_org_id UUID;
    target_user_id UUID;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'requests' THEN
            target_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
            target_user_id := COALESCE(NEW.created_by, OLD.created_by);
        WHEN 'tasks' THEN
            target_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
            SELECT created_by INTO target_user_id
            FROM requests
            WHERE id = COALESCE(NEW.request_id, OLD.request_id);
        WHEN 'user_files', 'user_file_revisions' THEN
            target_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
            target_user_id := COALESCE(NEW.user_id, OLD.user_id);
        WHEN 'user_interactions' THEN
            target_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
            target_user_id := COALESCE(NEW.user_id, OLD.user_id);
        WHEN 'user_interaction_messages' THEN
            SELECT organization_id, user_id INTO target_org_id, target_user_id
            FROM user_interactions
            WHERE id = COALESCE(NEW.interaction_id, OLD.interaction_id);
        WHEN 'agent_definitions', 'skill_definitions', 'mcp_server_configs' THEN
            target_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
            target_user_id := COALESCE(NEW.created_by, OLD.created_by);
    END CASE;

    IF target_org_id IS NOT NULL THEN
        PERFORM pg_notify(
            'lucent_live_update',
            jsonb_build_object(
                'organization_id', target_org_id,
                'user_id', target_user_id,
                'kind', TG_TABLE_NAME
            )::text
        );
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS live_web_requests_notify ON requests;
CREATE TRIGGER live_web_requests_notify
    AFTER INSERT OR UPDATE OR DELETE ON requests
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();

DROP TRIGGER IF EXISTS live_web_tasks_notify ON tasks;
CREATE TRIGGER live_web_tasks_notify
    AFTER INSERT OR UPDATE OR DELETE ON tasks
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();

DROP TRIGGER IF EXISTS live_web_files_notify ON user_files;
CREATE TRIGGER live_web_files_notify
    AFTER INSERT OR UPDATE OR DELETE ON user_files
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();

DROP TRIGGER IF EXISTS live_web_file_revisions_notify ON user_file_revisions;
CREATE TRIGGER live_web_file_revisions_notify
    AFTER INSERT ON user_file_revisions
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();

DROP TRIGGER IF EXISTS live_web_interactions_notify ON user_interactions;
CREATE TRIGGER live_web_interactions_notify
    AFTER INSERT OR UPDATE OR DELETE ON user_interactions
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();

DROP TRIGGER IF EXISTS live_web_interaction_messages_notify ON user_interaction_messages;
CREATE TRIGGER live_web_interaction_messages_notify
    AFTER INSERT ON user_interaction_messages
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();

DROP TRIGGER IF EXISTS live_web_agents_notify ON agent_definitions;
CREATE TRIGGER live_web_agents_notify
    AFTER INSERT OR UPDATE OR DELETE ON agent_definitions
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();

DROP TRIGGER IF EXISTS live_web_skills_notify ON skill_definitions;
CREATE TRIGGER live_web_skills_notify
    AFTER INSERT OR UPDATE OR DELETE ON skill_definitions
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();

DROP TRIGGER IF EXISTS live_web_mcp_servers_notify ON mcp_server_configs;
CREATE TRIGGER live_web_mcp_servers_notify
    AFTER INSERT OR UPDATE OR DELETE ON mcp_server_configs
    FOR EACH ROW EXECUTE FUNCTION notify_live_web_event();