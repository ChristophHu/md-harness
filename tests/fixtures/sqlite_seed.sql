-- Reproduzierbare Testdaten für die MD-Harness-SQLite-Datenbank.
INSERT INTO projects (id, name, path)
VALUES (100, 'Fixture-Projekt', '/tmp/md-harness-fixture');

INSERT INTO tasks (id, project_id, external_key, title, description, status, priority)
VALUES
    (1001, 100, 'FIX-001', 'Anforderungen prüfen', 'Markdown-Anforderungen auswerten', 'completed', 'high'),
    (1002, 100, 'FIX-002', 'Implementierung vorbereiten', 'Grundstruktur vorbereiten', 'ready', 'normal'),
    (1003, 100, 'FIX-003', 'Tests ausführen', 'Automatisierte Tests ausführen', 'created', 'low');

INSERT INTO task_dependencies (task_id, depends_on_task_id, dependency_type)
VALUES (1003, 1002, 'blocks');

INSERT INTO task_acceptance_criteria (task_id, criterion, completed)
VALUES (1002, 'Testdaten sind reproduzierbar', 0);
