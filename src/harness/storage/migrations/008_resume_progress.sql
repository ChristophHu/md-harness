ALTER TABLE task_checkpoints ADD COLUMN next_step_id TEXT;
ALTER TABLE task_checkpoints ADD COLUMN completed_step_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE task_checkpoints ADD COLUMN plan_fingerprint TEXT;
