CREATE TABLE IF NOT EXISTS task_checkpoints (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    next_action TEXT NOT NULL,
    plan_version INTEGER,
    attempt_id INTEGER,
    reason TEXT,
    context_data TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resumed_at TEXT
);
