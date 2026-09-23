ALTER TABLE task_checkpoints ADD COLUMN resume_claim_token TEXT;
ALTER TABLE task_checkpoints ADD COLUMN resume_claim_expires_at TEXT;
ALTER TABLE task_checkpoints ADD COLUMN waiting_reason_code TEXT;
CREATE INDEX IF NOT EXISTS idx_checkpoint_resume_lease
    ON task_checkpoints(resume_claim_expires_at);
