-- Align legacy task statuses with the engine workflow.
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;

ALTER TABLE tasks RENAME TO tasks_legacy;

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    parent_id INTEGER REFERENCES tasks(id),
    external_key TEXT UNIQUE,
    title TEXT NOT NULL,
    description TEXT,
    task_type TEXT NOT NULL DEFAULT 'task',
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'ready', 'planning', 'executing', 'validating', 'waiting', 'done', 'cancelled')),
    priority TEXT NOT NULL DEFAULT 'normal',
    approval_status TEXT NOT NULL DEFAULT 'pending' CHECK (approval_status IN ('pending', 'approved', 'rejected', 'revoked')),
    assigned_agent TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT
);

INSERT INTO tasks (id, project_id, parent_id, external_key, title, description, task_type, status, priority, approval_status, assigned_agent, created_at, updated_at, started_at, completed_at)
SELECT id, project_id, parent_id, external_key, title, description, task_type,
    CASE status
        WHEN 'idea' THEN 'created'
        WHEN 'backlog' THEN 'created'
        WHEN 'planned' THEN 'planning'
        WHEN 'in_progress' THEN 'executing'
        WHEN 'blocked' THEN 'waiting'
        WHEN 'review' THEN 'validating'
        WHEN 'testing' THEN 'validating'
        WHEN 'completed' THEN 'done'
        WHEN 'failed' THEN 'executing'
        ELSE status
    END,
    priority, approval_status, assigned_agent, created_at, updated_at, started_at, completed_at
FROM tasks_legacy;

DROP TABLE tasks_legacy;

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_executable ON tasks(approval_status, status, priority);

CREATE TRIGGER IF NOT EXISTS trg_tasks_updated_at
AFTER UPDATE OF title, description, task_type, status, priority ON tasks
FOR EACH ROW
BEGIN
    UPDATE tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
END;

PRAGMA foreign_keys = ON;
