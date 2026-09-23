CREATE TABLE IF NOT EXISTS task_replans (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_id INTEGER REFERENCES task_attempts(id),
    parent_plan_version INTEGER,
    plan_version INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    reason_fingerprint TEXT NOT NULL,
    plan_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_task_replans_task ON task_replans(task_id, id);
