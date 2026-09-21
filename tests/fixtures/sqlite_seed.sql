-- Reproduzierbare Testdaten für die MD-Harness-SQLite-Datenbank.
INSERT INTO projects (id, name, description, path)
VALUES (100, 'Fixture-Projekt', 'Beispielprojekt für den vollständigen Task-Lebenszyklus.', '/tmp/md-harness-fixture');

INSERT INTO projects (id, parent_id, name, description, path)
VALUES (101, 100, 'Fixture-Teilprojekt', 'Beispielhafte fachliche Unterteilung.', '/tmp/md-harness-fixture/subproject');

INSERT INTO tasks (id, project_id, external_key, title, description, status, priority)
VALUES
    (1001, 100, 'FIX-001', 'Anforderungen prüfen', 'Markdown-Anforderungen auswerten', 'completed', 'high'),
    (1002, 100, 'FIX-002', 'Implementierung vorbereiten', 'Grundstruktur vorbereiten', 'ready', 'normal'),
    (1003, 100, 'FIX-003', 'Tests ausführen', 'Automatisierte Tests ausführen', 'created', 'low');

INSERT INTO tasks (id, project_id, parent_id, external_key, title, description, task_type, status, priority, approval_status, assigned_agent)
VALUES (1004, 101, 1002, 'FIX-004', 'Coverage prüfen', 'Testabdeckung des Teilprojekts prüfen', 'subtask', 'ready', 'normal', 'approved', 'developer');

INSERT INTO agents (id, name, description, capabilities)
VALUES (200, 'developer', 'Implementiert und testet Python-Code.', '["python", "testing"]');

INSERT INTO task_dependencies (task_id, depends_on_task_id, dependency_type)
VALUES (1003, 1002, 'blocks');

INSERT INTO task_acceptance_criteria (task_id, criterion, completed)
VALUES (1002, 'Testdaten sind reproduzierbar', 0);

INSERT INTO task_test_criteria (task_id, criterion, test_type, command)
VALUES (1004, 'Coverage beträgt mindestens 90 Prozent', 'coverage', 'uv run pytest');

INSERT INTO task_approvals (task_id, status, approved_by, reason)
VALUES (1004, 'approved', 'reviewer', 'Scope und Akzeptanzkriterien geprüft');

INSERT INTO task_assignments (task_id, agent_id, assignment_type)
VALUES (1004, 200, 'execution');

INSERT INTO task_events (task_id, event_type, payload)
VALUES (1004, 'task.approved', '{"approved_by":"reviewer"}');
