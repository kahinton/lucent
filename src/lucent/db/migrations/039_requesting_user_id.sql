ALTER TABLE tasks ADD COLUMN requesting_user_id UUID REFERENCES users(id);

-- Backfill from requests.created_by
UPDATE tasks
SET requesting_user_id = r.created_by
FROM requests r
WHERE tasks.request_id = r.id
  AND tasks.requesting_user_id IS NULL;

-- Index for filtering
CREATE INDEX idx_tasks_requesting_user
    ON tasks(requesting_user_id)
    WHERE requesting_user_id IS NOT NULL;
