-- Migration: Extend audit log action types
-- Adds 'system_cleanup' to the allowed action_type values

ALTER TABLE memory_audit_log
    DROP CONSTRAINT memory_audit_log_action_type_check;

ALTER TABLE memory_audit_log
    ADD CONSTRAINT memory_audit_log_action_type_check
    CHECK (action_type IN (
        'create',
        'update',
        'delete',
        'restore',
        'share',
        'unshare',
        'hard_delete',
        'system_cleanup'
    ));
