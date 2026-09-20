-- Initial version marker for the MD Harness SQLite schema.
-- The complete idempotent schema is applied from ../schema.sql first.
CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_metadata (key, value)
VALUES ('schema_name', 'md-harness');
