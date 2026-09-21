# storage

Die Storage-Schicht verwaltet die relationale Persistenz des Harnesses. SQLite ist die operative Quelle für Projekte, Aufgaben, Status, Abhängigkeiten, Ereignisse, Agentenversuche und Artefakte.

## Grundprinzip

```text
Engine und Stores
        ↓
SQLite-Datenbank: state/harness.sqlite
        ↓
Aufgaben, Status, Abhängigkeiten, Events und Artefakte
```

Der Vault bleibt die Quelle für menschlich gepflegtes Wissen. SQLite ist dagegen die verbindliche Quelle für den operativen Aufgaben- und Ausführungsstatus.

## Inhalt

- `database.py`: Verbindungen, Schema-Laden, Migrationen, Transaktionen, Healthcheck, Schema-Versionierung und Backup/Wiederherstellung
- `schema.sql`: vollständiges, idempotentes Initialschema mit Tabellen, Indizes und Triggern
- `project_store.py`: Projekte anlegen, laden und auflisten
- `task_store.py`: Aufgaben, Aktualisierungen, Statuswechsel, Freigaben, Abhängigkeiten, Akzeptanz- und Testkriterien sowie Agentenversuche
- `event_store.py`: Aufgabenereignisse protokollieren und abfragen
- `artifact_store.py`: erzeugte Artefakte registrieren und abfragen
- `migrations/`: versionierte SQL-Migrationen im Format `NNN_name.sql`
- `__init__.py`: Storage-Paket

## Datenmodell

Das Schema umfasst:

- `projects`
- `tasks`
- `task_dependencies`
- `task_acceptance_criteria`
- `task_test_criteria`
- `task_approvals`
- `agents`
- `task_assignments`
- `task_attempts`
- `task_events`
- `task_artifacts`

Projekte können über `projects.parent_id` hierarchisch verschachtelt werden. Aufgaben können über `tasks.parent_id` in Epics, Features, Tasks und atomare Subtasks zerlegt werden. `approval_status` ist bewusst vom operativen `status` getrennt: Ein Task darf nur nach expliziter Freigabe ausführbar werden.

Zusätzlich existieren Indizes für Status/Priorität, Projekte, Events, Agentenversuche und Artefakte. Ein Trigger aktualisiert `tasks.updated_at` bei relevanten Änderungen automatisch.

## Migrationen und Versionierung

Neue Datenbanken werden aus `schema.sql` initialisiert. Danach werden ausstehende Migrationen aus `migrations/` anhand ihrer dreistelligen Versionsnummer ausgeführt. Die aktuelle Version wird in SQLite über `PRAGMA user_version` gespeichert.

Bestehende Migrationen werden nicht verändert. Schemaänderungen erhalten eine neue Datei, beispielsweise `002_add_memory_table.sql`.

## Transaktionen und Sicherheit

Transaktionen werden über `database.transaction()` ausgeführt. Bei einem SQLite-Fehler erfolgt ein Rollback; nach erfolgreichem Abschluss wird committed. SQL-Zugriffe der Stores verwenden gebundene Parameter. Dynamische Spaltennamen werden gegen erlaubte Feldlisten geprüft.

## Backup und Wiederherstellung

`backup_database()` erzeugt ein SQLite-Backup über die Online-Backup-API. `restore_database()` stellt ein Backup in einer Zieldatei wieder her. `healthcheck()` prüft die Datenbank mit SQLite `quick_check()`.

## Tests

Die Storage-Schicht wird durch `tests/unit/test_storage_stores.py` und `tests/unit/test_database.py` geprüft. Reproduzierbare Testdaten liegen in `tests/fixtures/sqlite_seed.sql` und enthalten ein Projekt mit drei Tasks, einer Abhängigkeit und einem Akzeptanzkriterium.

Der vollständige Testlauf erfolgt mit:

```bash
uv run pytest
```

Die aktuelle Storage- und Gesamt-Coverage beträgt 100 %.

## Ausführbare Tasks

`TaskStore.get_executable_tasks()` liefert nur freigegebene (`approval_status = approved`), vorbereitete (`ready` oder `planned`) und nicht durch offene Blocker-Abhängigkeiten gesperrte Tasks. Optional kann nach einem zugewiesenen Agenten gefiltert werden.

Die bisherige lokale Datenbankstruktur gilt für diese Modellversion als verworfen. Eine neue Datenbank wird aus dem aktuellen `schema.sql` initialisiert; bestehende lokale Datenbankdateien werden nicht automatisch migriert oder gelöscht.
