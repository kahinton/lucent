-- Migration 113: Handoffs are strictly per-user private — fail closed.
--
-- Kyle's standing directive (2026-09-12): handoffs are strictly per-user
-- private; the org-addressable handoff class is NOT needed or wanted.
-- Every handoff (user_interactions row) must carry a concrete owner user;
-- there is no org-broadcast class.
--
-- Adds CHECK (user_id IS NOT NULL) via NOT VALID + VALIDATE to avoid a long
-- table lock: NOT VALID installs the constraint for future writes instantly
-- without scanning existing rows, then VALIDATE walks the table under a
-- weaker lock to prove existing rows comply.

ALTER TABLE user_interactions
    ADD CONSTRAINT ck_user_interactions_user_not_null
    CHECK (user_id IS NOT NULL) NOT VALID;

ALTER TABLE user_interactions
    VALIDATE CONSTRAINT ck_user_interactions_user_not_null;

COMMENT ON CONSTRAINT ck_user_interactions_user_not_null ON user_interactions IS
  'Handoffs are strictly per-user private: every row is owned by a concrete user. Enforced here so a NULL user_id can never silently become an org-wide broadcast.';