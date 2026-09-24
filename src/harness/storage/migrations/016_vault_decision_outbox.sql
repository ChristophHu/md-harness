ALTER TABLE projects ADD COLUMN project_key TEXT;
UPDATE projects SET project_key = lower(hex(randomblob(16))) WHERE project_key IS NULL;
CREATE UNIQUE INDEX idx_projects_project_key ON projects(project_key);
CREATE TRIGGER trg_projects_assign_project_key AFTER INSERT ON projects
WHEN NEW.project_key IS NULL
BEGIN
    UPDATE projects SET project_key = lower(hex(randomblob(16))) WHERE id = NEW.id;
END;
CREATE TRIGGER trg_projects_immutable_project_key BEFORE UPDATE OF project_key ON projects
WHEN OLD.project_key IS NOT NULL AND NEW.project_key IS NOT OLD.project_key
BEGIN
    SELECT RAISE(ABORT, 'project_key is immutable');
END;

CREATE TABLE vault_decision_outbox (
    interaction_id TEXT PRIMARY KEY REFERENCES task_human_interactions(interaction_id) ON DELETE CASCADE,
    project_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'publishing', 'published', 'blocked')),
    claim_token TEXT,
    claim_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    published_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_vault_decision_outbox_status ON vault_decision_outbox(status, claim_expires_at);
