ALTER TABLE task_checkpoints ADD COLUMN wait_token TEXT;
ALTER TABLE task_checkpoints ADD COLUMN external_resolved_at TEXT;
ALTER TABLE task_checkpoints ADD COLUMN resolved_by TEXT;
ALTER TABLE task_checkpoints ADD COLUMN information_ref TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_checkpoint_wait_token
    ON task_checkpoints(wait_token) WHERE wait_token IS NOT NULL;
