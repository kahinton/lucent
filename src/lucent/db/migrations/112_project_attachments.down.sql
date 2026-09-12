-- Rollback migration 112. Refuses rollback while any memory or handoff is
-- still attached to a project — rollback would silently un-file them.
--
-- lucent: warning=detaches memories/handoffs from projects (columns dropped)

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM memories WHERE project_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM user_interactions WHERE project_id IS NOT NULL) THEN
        RAISE EXCEPTION
            'Cannot roll back project-attachment migration while attached memories or handoffs exist';
    END IF;
END $$;

DROP INDEX IF EXISTS idx_user_interactions_project;
DROP INDEX IF EXISTS idx_memories_project;

ALTER TABLE user_interactions DROP COLUMN IF EXISTS project_id;
ALTER TABLE memories DROP COLUMN IF EXISTS project_id;