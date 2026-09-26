CREATE TABLE IF NOT EXISTS agent_task_bindings (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    profile_name TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    profile_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
