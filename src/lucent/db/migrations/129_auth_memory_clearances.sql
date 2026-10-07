-- Migration 129: Memories join the clearance system.
--
-- Memory *use* (search, detail reads, tags, export) moves to
-- clearance-driven, default-deny reads on `memories`; the legacy
-- `memory_access_grants` table is retired in favor of `auth_clearances`
-- (org/user/group read clearances), keeping `memories.shared` as a
-- maintained cosmetic projection.
--
-- The big data shift: memories authored by the daemon (daemon-role user_id,
-- or legacy 'Lucent Daemon' username + daemon tag) are reassigned, one time,
-- to each org's first owner as a REAL user. Per product decision the
-- reassigned corpus becomes private to that owner: no org read clearance is
-- stamped for daemon-authored rows, so the grant conversion below filters
-- them out.
--
-- Migration 123 already gave `memories` the owner-clearance INSERT/UPDATE
-- triggers on `user_id` (transfer fires on the reassignment UPDATE), and the
-- "memory" resource spec already exists in AUTH_ACCESS_RESOURCES.
--
-- Order matters: grants convert BEFORE reassignment so daemon-authored rows
-- never receive an org clearance.

-- ---------------------------------------------------------------------------
-- 1. Legacy grant conversion -> read clearances (daemon-authored excluded).
-- `type='individual'` rows are skipped entirely: private-by-rule since 103,
-- the individual row is only a tombstone against further sharing.
-- The daemon predicate COALESCEs the user_id arm so NULL-owner rows are not
-- dropped by three-valued logic.
INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id, granted_by)
SELECT m.auth_id, 'read', 'org', g.organization_id, g.created_by
FROM memory_access_grants g
JOIN memories m ON m.id = g.memory_id
WHERE g.grantee_type = 'organization'
  AND m.organization_id IS NOT NULL
  AND NOT (
      COALESCE(
          m.user_id IN (SELECT id FROM users WHERE role = 'daemon'), FALSE
      )
      OR (m.username = 'Lucent Daemon' AND 'daemon' = ANY(m.tags))
  )
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id, granted_by)
SELECT m.auth_id, 'read', 'user', g.grantee_user_id, g.created_by
FROM memory_access_grants g
JOIN memories m ON m.id = g.memory_id
WHERE g.grantee_type = 'user'
  AND g.grantee_user_id IS NOT NULL
  AND g.grantee_user_id IS DISTINCT FROM m.user_id
  AND NOT (
      COALESCE(
          m.user_id IN (SELECT id FROM users WHERE role = 'daemon'), FALSE
      )
      OR (m.username = 'Lucent Daemon' AND 'daemon' = ANY(m.tags))
  )
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id, granted_by)
SELECT m.auth_id, 'read', 'group', g.grantee_group_id, g.created_by
FROM memory_access_grants g
JOIN memories m ON m.id = g.memory_id
WHERE g.grantee_type = 'group'
  AND g.grantee_group_id IS NOT NULL
  AND NOT (
      COALESCE(
          m.user_id IN (SELECT id FROM users WHERE role = 'daemon'), FALSE
      )
      OR (m.username = 'Lucent Daemon' AND 'daemon' = ANY(m.tags))
  )
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. Reassign daemon-owned memories to each org's first owner as a real
-- user. The 123 transfer trigger files the owner clearance automatically as
-- user_id changes. `username` is rewritten to the new owner's display name
-- so legacy 'Lucent Daemon' classification arms stop matching.
-- ---------------------------------------------------------------------------
WITH first_owner AS (
    SELECT DISTINCT ON (organization_id)
           organization_id,
           id AS owner_user_id,
           display_name
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
)
UPDATE memories m
SET user_id = org.owner_user_id,
    username = org.display_name
FROM first_owner org
WHERE m.organization_id = org.organization_id
  AND (
      m.user_id IN (SELECT id FROM users WHERE role = 'daemon')
      OR (m.username = 'Lucent Daemon' AND 'daemon' = ANY(m.tags))
  );

-- Legacy-tagged rows with a REAL non-daemon owner: rewrite the stale
-- 'Lucent Daemon' username to the actual owner's display name.
UPDATE memories m
SET username = u.display_name
FROM users u
WHERE m.user_id = u.id
  AND m.username = 'Lucent Daemon'
  AND 'daemon' = ANY(m.tags);

-- ---------------------------------------------------------------------------
-- 3. Orphan repair (expected dev drift: 2 rows).
-- 3a. Row has an owner but no organization: adopt the owner's org.
-- ---------------------------------------------------------------------------
UPDATE memories m
SET organization_id = u.organization_id
FROM users u
WHERE m.user_id = u.id
  AND m.organization_id IS NULL
  AND u.organization_id IS NOT NULL;

-- 3b. Daemon-classified rows newly attached to an org (user_id NULL after
-- 3a): same reassignment as section 2, which only reached org-attached rows.
WITH first_owner AS (
    SELECT DISTINCT ON (organization_id)
           organization_id,
           id AS owner_user_id,
           display_name
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
)
UPDATE memories m
SET user_id = org.owner_user_id,
    username = org.display_name
FROM first_owner org
WHERE m.organization_id = org.organization_id
  AND (
      (m.user_id IS NOT NULL AND m.user_id IN (SELECT id FROM users WHERE role = 'daemon'))
      OR (m.username = 'Lucent Daemon' AND 'daemon' = ANY(m.tags))
  );

-- 3c. Any remaining ownerless row with an org: adopt the org's first owner
-- (transfer trigger files the clearance on the NULL -> uuid change).
WITH first_owner AS (
    SELECT DISTINCT ON (organization_id)
           organization_id, id AS owner_user_id
    FROM users
    WHERE role = 'owner' AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
)
UPDATE memories m
SET user_id = org.owner_user_id
FROM first_owner org
WHERE m.user_id IS NULL
  AND m.organization_id = org.organization_id;

-- ---------------------------------------------------------------------------
-- 4. Owner-clearance safety backfill: every row with a set user_id holds its
-- 'owner','user' clearance (drift expected: 0; 123 backfilled and triggers
-- maintain it).
-- ---------------------------------------------------------------------------
INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id, granted_by)
SELECT m.auth_id, 'owner', 'user', m.user_id, m.user_id
FROM memories m
WHERE m.user_id IS NOT NULL
  AND m.auth_id IS NOT NULL
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;

-- ---------------------------------------------------------------------------
-- 5. Retire the legacy grants table (clearances + the shared projection
-- carry the semantics from here).
-- ---------------------------------------------------------------------------
DROP TABLE memory_access_grants;
-- ---------------------------------------------------------------------------
-- 6. Daemon self-read backfill: legacy daemon-tagged memories (e.g. the
-- environment assessment) keep a read clearance for the org's daemon
-- service identity so the daemon's org_shared_only searches still reach
-- what it authored. Reassigned rows stay owner-private: no org clearance
-- is filed and org members are untouched (the clearance names only the
-- daemon principal).
-- ---------------------------------------------------------------------------
WITH daemon_user AS (
    SELECT DISTINCT ON (organization_id)
           organization_id, id AS daemon_user_id
    FROM users
    WHERE role = 'daemon'
      AND organization_id IS NOT NULL
    ORDER BY organization_id, created_at
)
INSERT INTO auth_clearances (auth_id, role, principal_type, principal_id, granted_by)
SELECT m.auth_id, 'read', 'user', du.daemon_user_id, m.user_id
FROM memories m
JOIN daemon_user du ON m.organization_id = du.organization_id
WHERE m.organization_id IS NOT NULL
  AND 'daemon' = ANY(m.tags)
ON CONFLICT (auth_id, principal_type, principal_id, role)
DO NOTHING;
