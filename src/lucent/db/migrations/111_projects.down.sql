-- Rollback migration 111. Refuses rollback while any chat, message, or file is
-- still filed in a project — rollback would silently un-file them.
--
-- lucent: warning=drops the projects table; project definitions are lost

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM llm_sessions WHERE project_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM llm_messages WHERE project_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM user_files WHERE project_id IS NOT NULL) THEN
        RAISE EXCEPTION
            'Cannot roll back Projects migration while filed chats, messages, or files exist';
    END IF;
END $$;

DROP INDEX IF EXISTS idx_llm_messages_project;
DROP INDEX IF EXISTS idx_llm_sessions_project;
DROP INDEX IF EXISTS idx_user_files_project;

ALTER TABLE llm_messages DROP COLUMN IF EXISTS project_id;
ALTER TABLE llm_sessions DROP COLUMN IF EXISTS project_id;
ALTER TABLE user_files DROP COLUMN IF EXISTS project_id;

DROP TABLE IF EXISTS projects;