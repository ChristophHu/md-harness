CREATE TABLE task_human_interactions (
    interaction_id TEXT PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    wait_token TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('information_request', 'human_decision', 'plan_review')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'answered', 'rejected', 'cancelled', 'expired', 'superseded')),
    prompt TEXT NOT NULL,
    response_schema TEXT NOT NULL,
    request_data TEXT NOT NULL DEFAULT '{}',
    response_data TEXT,
    response_ref TEXT,
    resume_action TEXT NOT NULL CHECK (resume_action IN ('retry_execution', 'replan', 'validate')),
    plan_version INTEGER,
    plan_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT,
    decided_by TEXT,
    UNIQUE(task_id, wait_token)
);
CREATE INDEX idx_human_interactions_task_status
    ON task_human_interactions(task_id, status, created_at);
