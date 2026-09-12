-- Migration 111: Projects — named workspaces grouping chats, files, and
-- standing instructions.
--
-- A project is a user-owned workspace. Membership is a nullable project_id on
-- llm_sessions (authoritative), llm_messages (denormalized copy for
-- message-phase context scoping, maintained at message insert), and user_files
-- (durable files filed into the workspace). NULL project_id everywhere means
-- "unfiled": existing chats and files keep today's behavior exactly and only
-- gain the ability to be filed. Deleting a project un-files (SET NULL) rather
-- than deleting anything.
--
-- organization_id is carried on projects so every project query can be
-- org+user scoped at the data layer, matching llm_sessions and user_files;
-- tenant isolation is enforced by construction, not by query discipline.

CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(256) NOT NULL,
    instructions TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_projects_name_nonempty CHECK (length(trim(name)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_projects_org_user_recent
    ON projects (organization_id, user_id, updated_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_projects_org_updated
    ON projects (organization_id, updated_at DESC NULLS LAST);

ALTER TABLE llm_sessions
    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE llm_messages
    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_llm_sessions_project
    ON llm_sessions (project_id, last_message_at DESC NULLS LAST)
    WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_messages_project
    ON llm_messages (project_id, sequence DESC)
    WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_user_files_project
    ON user_files (project_id, created_at DESC)
    WHERE project_id IS NOT NULL AND deleted_at IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lucent_daemon') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON projects TO lucent_daemon';
    END IF;
END $$;

COMMENT ON TABLE projects IS
    'Named workspaces grouping chats, durable files, and standing instructions. All access is scoped to organization_id plus user_id.';
COMMENT ON COLUMN projects.instructions IS
    'Standing workspace instructions injected as context in every chat that belongs to the project, composed alongside user/agent-level context.';
COMMENT ON COLUMN llm_sessions.project_id IS
    'Authoritative project membership. NULL = unfiled. Set NULL on project delete.';
COMMENT ON COLUMN llm_messages.project_id IS
    'Denormalized copy of the owning session''s project for message-phase scoping; llm_sessions.project_id is authoritative.';
COMMENT ON COLUMN user_files.project_id IS
    'Project the durable file is filed into. NULL = unfiled. Set NULL on project delete.';