-- Undo 132: restore the shared column as a cosmetic projection, backfilled
-- from the org read clearances (the source of truth while the column was
-- gone). Recreates both partial indexes 004/010 defined on it.

ALTER TABLE memories
    ADD COLUMN shared boolean NOT NULL DEFAULT false;

-- Re-project: a memory is "shared" exactly when it carries ('read','org').
UPDATE memories m
SET shared = true
WHERE EXISTS (
    SELECT 1 FROM auth_clearances c
    WHERE c.auth_id = m.auth_id
      AND c.role = 'read'
      AND c.principal_type = 'org'
);

COMMENT ON COLUMN memories.shared IS
    'Legacy compatibility projection — maintained by the grant writes.';

-- Recreate 004's and 010's partial indexes exactly as originally defined.
CREATE INDEX idx_memories_org_shared
    ON memories (organization_id, shared)
    WHERE deleted_at IS NULL AND shared = true;

CREATE INDEX idx_memories_org_shared_active
    ON memories (organization_id)
    WHERE deleted_at IS NULL AND shared = true;

COMMENT ON INDEX idx_memories_org_shared_active IS 'Optimizes access control queries for shared memories';