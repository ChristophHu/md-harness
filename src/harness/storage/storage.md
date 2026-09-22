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
- `agent_store.py`: Agenten registrieren und Task-Zuweisungen verwalten
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

Zusätzlich existieren Indizes für Status/Priorität, Projekt- und Task-Hierarchien, ausführbare Tasks, Events, Agentenversuche, Kriterien und Artefakte. Trigger aktualisieren `tasks.updated_at` und `projects.updated_at` bei relevanten Änderungen automatisch. `planning_started_at`, `validation_started_at` und `failed_at` markieren die jeweiligen Prozessphasen.

## Transaktionen

`TransactionManager` koordiniert atomare SQLite-Blöcke für die Stores. `TaskStore`, `EventStore` und `ArtifactStore` müssen dafür dieselbe `sqlite3.Connection` verwenden. Erfolgreiche Blöcke werden committed; bei Exceptions erfolgt ein Rollback. Nach einem Rollback kann eine separate Fehlertransaktion `task.persistence.failed` und den fehlgeschlagenen Taskstatus persistieren.

`StoreFactory.create(connection)` erzeugt dafür ein vollständiges `StoreBundle` mit allen drei Stores und dem zugehörigen `TransactionManager`. Der Orchestrator kann dieses Bundle über `stores=` erhalten und akzeptiert dann keine parallel übergebenen Einzel-Stores.

`EngineUnitOfWork` liegt fachlich über dem `TransactionManager`. Sie bündelt die Persistenz eines Cycle-Starts, eines Execution-Ergebnisses und einer Workflow-Entscheidung jeweils in einer Transaktion. Die eigentliche Toolausführung bleibt außerhalb der Transaktion, da externe Seiteneffekte nicht durch SQLite zurückgerollt werden können.

Der operative Lebenszyklus bildet den Engine-Workflow direkt ab:

```text
created → ready → planning → executing → validating → done
```

Zusätzliche Übergänge bilden Fehlerbehandlung und Unterbrechungen ab:

```text
validating → executing   (Ausführungsfehler)
validating → planning    (Planungsfehler)
* → waiting              (externe Voraussetzung oder Freigabe fehlt)
* → failed               (nicht behebbarer Fehler oder Limit erreicht)
```

Ein Task kann unabhängig davon freigegeben oder zurückgezogen werden. Die Freigabe wird in `task_approvals` historisiert; `tasks.approval_status` enthält den aktuellen Freigabestatus.

## Migrationen und Versionierung

Neue Datenbanken werden aus `schema.sql` initialisiert. Danach werden ausstehende Migrationen aus `migrations/` anhand ihrer dreistelligen Versionsnummer ausgeführt. Die aktuelle Version wird in SQLite über `PRAGMA user_version` gespeichert.

Bestehende Migrationen werden nicht verändert. Schemaänderungen erhalten eine neue Datei, beispielsweise `002_add_memory_table.sql`. Die aktuelle Modellversion wird als neue Ausgangsbasis initialisiert; eine automatische Konvertierung der verworfenen alten lokalen Struktur ist nicht vorgesehen.

Schema-Version 3 ergänzt den Taskstatus `failed`. Der Orchestrator verwaltet außerdem `task_attempts`: Jeder Workflow-Zyklus wird begonnen, bei Retry/Replan als fehlgeschlagen abgeschlossen und bei Erfolg, Waiting oder endgültigem Fehler abgeschlossen.

## Transaktionen und Sicherheit

Transaktionen werden über `database.transaction()` ausgeführt. Bei einem SQLite-Fehler erfolgt ein Rollback; nach erfolgreichem Abschluss wird committed. SQL-Zugriffe der Stores verwenden gebundene Parameter. Dynamische Spaltennamen werden gegen erlaubte Feldlisten geprüft.

## Backup und Wiederherstellung

`backup_database()` erzeugt ein SQLite-Backup über die Online-Backup-API. `restore_database()` stellt ein Backup in einer Zieldatei wieder her. `healthcheck()` prüft die Datenbank mit SQLite `quick_check()`.

## Tests

Die Storage-Schicht wird durch `tests/unit/test_storage_stores.py` und `tests/unit/test_database.py` geprüft. Reproduzierbare Beispieldaten liegen in `tests/fixtures/sqlite_seed.sql`. Sie enthalten ein Hauptprojekt mit Unterprojekt, vier hierarchische Tasks, eine Abhängigkeit, Akzeptanz- und Testkriterien, einen Agenten, eine Zuweisung, eine Freigabe und ein Event.

Der vollständige Testlauf erfolgt mit:

```bash
uv run pytest
```

Die aktuelle Storage- und Gesamt-Coverage beträgt 100 %.

## Ausführbare Tasks

`TaskStore.get_executable_tasks()` liefert nur freigegebene (`approval_status = approved`), vorbereitete (`ready` oder `planning`) und nicht durch offene Blocker-Abhängigkeiten gesperrte Tasks. Optional kann nach einem zugewiesenen Agenten gefiltert werden.

Die bisherige lokale Datenbankstruktur gilt für diese Modellversion als verworfen. Eine neue Datenbank wird aus dem aktuellen `schema.sql` initialisiert; bestehende lokale Datenbankdateien werden nicht automatisch migriert oder gelöscht.

`CheckpointStore` persistiert Resume-Punkte wartender Tasks mit Phase, nächster Aktion, Attempt, Grund und Zeitpunkten. `Orchestrator.resume()` markiert einen vorhandenen Checkpoint als wieder aufgenommen und startet den Task erneut mit dem gespeicherten Kontext.
