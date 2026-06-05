-- Migration: Add definition audit event action_types
-- Extends the action_type CHECK constraint with 7 new values for
-- agent/skill/MCP server definition lifecycle events.

ALTER TABLE memory_audit_log
    DROP CONSTRAINT memory_audit_log_action_type_check;

ALTER TABLE memory_audit_log
    ADD CONSTRAINT memory_audit_log_action_type_check
    CHECK (action_type IN (
        -- Existing types
        'create',
        'update',
        'delete',
        'restore',
        'share',
        'unshare',
        'hard_delete',
        'system_cleanup',
        -- Integration security events (dedicated action_types)
        'signature_verification_failed',
        'channel_not_allowed',
        'challenge_failed',
        'resolution_failed',
        'integration_rate_limited',
        'integration_revoked',
        'link_revoked',
        -- Integration operational events (shared action_type, details in context JSONB)
        'integration_event',
        -- Definition lifecycle events
        'definition_create',
        'definition_update',
        'definition_approve',
        'definition_reject',
        'definition_delete',
        'definition_grant',
        'definition_revoke'
    ));
