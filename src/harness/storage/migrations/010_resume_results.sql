ALTER TABLE task_checkpoints ADD COLUMN execution_result TEXT;
ALTER TABLE task_checkpoints ADD COLUMN validation_result TEXT;
ALTER TABLE task_checkpoints ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task_checkpoints ADD COLUMN invalidated_at TEXT;
