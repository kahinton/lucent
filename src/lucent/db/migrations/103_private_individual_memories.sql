-- Migration 103: Keep account-linked individual memories private
--
-- Individual memories contain account profile and contact information. They
-- must never be organization-shared by default. This corrects profiles created
-- before UserRepository began explicitly marking them private.

UPDATE memories
SET shared = false
WHERE type = 'individual'
  AND shared = true;