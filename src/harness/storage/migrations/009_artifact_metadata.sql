ALTER TABLE task_artifacts ADD COLUMN size_bytes INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_task_path_checksum
    ON task_artifacts(task_id, path, checksum);
