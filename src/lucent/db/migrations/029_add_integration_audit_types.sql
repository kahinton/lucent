-- Migration: Add integration audit event action_types
-- Extends the action_type CHECK constraint with 8 new values:
-- 7 dedicated security event types + 1 shared operational type (integration_event).

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
        'integration_event'
    ));
