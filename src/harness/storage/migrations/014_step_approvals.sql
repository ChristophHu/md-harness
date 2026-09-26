CREATE TABLE task_step_approvals (
    wait_token TEXT PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    plan_version INTEGER NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    step_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments_fingerprint TEXT NOT NULL,
    permission_scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'revoked')),
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_step_approvals_task ON task_step_approvals(task_id, status);
