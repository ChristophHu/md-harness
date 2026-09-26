CREATE TABLE task_tool_invocations (
    invocation_id TEXT PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    plan_fingerprint TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_fingerprint TEXT NOT NULL,
    permission_scope TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'no_effect')),
    run_count INTEGER NOT NULL DEFAULT 1,
    step_result TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    evidence_ref TEXT,
    UNIQUE(task_id, plan_fingerprint, step_id)
);
CREATE INDEX idx_tool_invocations_task_status
    ON task_tool_invocations(task_id, status);
