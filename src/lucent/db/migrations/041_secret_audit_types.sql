-- Migration: Add secret audit event action_types
-- Extends the action_type CHECK constraint with 3 new values for
-- secret storage lifecycle events (create, read, delete).

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
        -- Integration security events
        'signature_verification_failed',
        'channel_not_allowed',
        'challenge_failed',
        'resolution_failed',
        'integration_rate_limited',
        'integration_revoked',
        'link_revoked',
        -- Integration operational events
        'integration_event',
        -- Definition lifecycle events
        'definition_create',
        'definition_update',
        'definition_approve',
        'definition_reject',
        'definition_delete',
        'definition_grant',
        'definition_revoke',
        -- Secret storage events
        'secret_create',
        'secret_read',
        'secret_delete'
    ));
