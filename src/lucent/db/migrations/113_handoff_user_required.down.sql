-- Migration 113 down: remove the per-user-private CHECK on user_interactions.
-- Restores the (pre-hardening) possibility of NULL user_id rows; the
-- application-layer guard in create_interaction remains.

ALTER TABLE user_interactions
    DROP CONSTRAINT IF EXISTS ck_user_interactions_user_not_null;