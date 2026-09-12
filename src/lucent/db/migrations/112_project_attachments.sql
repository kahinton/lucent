-- Migration 112: Project attachments — memories and handoffs join projects.
--
-- Extends the Projects membership model (migration 111) from chats and files
-- to memories and handoffs: nullable project_id on memories and
-- user_interactions, same single-membership pattern as llm_sessions and
-- user_files. NULL = unfiled; existing rows keep today's behavior exactly and
-- only gain the ability to be attached. Deleting a project un-files
-- (SET NULL) — it never deletes memories or handoffs.
--
-- memories carries (user_id, organization_id) since migrations 002/004 and
-- user_interactions carries (organization_id, user_id) since migration 084,
-- so project attachment stays (org, user)-scoped at the data layer: projects
-- are (org, user)-owned (migration 111) and every repository path verifies
-- the target project AND the member row against the caller's scope pair
-- before any UPDATE — a cross-owner or cross-org attach cannot be expressed.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE user_interactions
    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_memories_project
    ON memories (project_id, updated_at DESC)
    WHERE project_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_interactions_project
    ON user_interactions (project_id, updated_at DESC)
    WHERE project_id IS NOT NULL;

COMMENT ON COLUMN memories.project_id IS
    'Project this memory is filed into. NULL = unfiled. Set NULL on project delete.';
COMMENT ON COLUMN user_interactions.project_id IS
    'Project this handoff/interaction is filed into. NULL = unfiled. Set NULL on project delete.';