# tools

Der Ordner enthält die standardisierten Werkzeuge, die das Harness seinen Agenten und dem Orchestrator zur Verfügung stellt.

## Gemeinsames Tool-Interface

### `base.py`

Definiert die gemeinsame Grundlage für alle Harness-Tools:

- `Tool`: abstrakte Basisklasse mit einheitlicher `execute()`-Methode
- `ToolDefinition`: Name, Beschreibung, Berechtigungsstufe und Parameter
- `ToolParameter`: Beschreibung und Pflichtstatus einzelner Parameter
- `PermissionLevel`: `read`, `write`, `destructive` und `network`
- `ToolContext`: Workspace, Dry-Run-Modus und zusätzliche Metadaten
- `ToolRegistry`: Registrierung, Suche, Validierung und Ausführung von Tools
- `ToolError`: einheitlicher Fehler für Registrierung und Argumentvalidierung

Die Registry ermöglicht eine einheitliche Nutzung:

```python
registry.execute(
    "sqlite",
    operation="fetch_all",
    sql="SELECT * FROM tasks",
)
```

## Konkrete Tools

### `filesystem.py`

Stellt kontrollierte Datei- und Ordneroperationen innerhalb eines festgelegten Workspace bereit.

Unterstützt unter anderem:

- Pfadauflösung mit Schutz vor Zugriff außerhalb des Workspace
- Dateien lesen und schreiben
- Verzeichnisse erstellen
- Dateien und Ordner auflisten
- Kopieren und Verschieben
- kontrolliertes Löschen
- Prüfung von Existenz, Datei- und Verzeichnisstatus

Destruktive Löschoperationen sind standardmäßig deaktiviert und müssen ausdrücklich erlaubt werden.

### `git.py`

Stellt kontrollierte Git-Repository-Operationen bereit.

Unterstützt unter anderem:

- Repository initialisieren
- Repository klonen
- Status und Branch prüfen
- Dateien vollständig oder selektiv stagen
- Commits erstellen
- Pull und Push
- Fetch, Merge und Rebase
- Branches wechseln oder erstellen
- Branches löschen; geschützte Haupt- und Entwicklungsbranches werden dabei abgelehnt
- Diffs, Diff-Statistiken und geänderte Dateien prüfen
- Arbeitsverzeichnis und letzten Commit prüfen
- Tags erstellen, löschen und anzeigen
- Commit-Historie anzeigen
- Remotes hinzufügen und anzeigen

Netzwerkoperationen wie Pull und Push müssen durch die Sicherheits- und Ausführungsregeln des Harnesses kontrolliert werden.

Alle vom Harness ausgeführten Commits, Pushes, Branch-Erstellungen und Merges
werden zusätzlich durch `security/gitflow_policy.py` gegen die Gitflow-Regeln
validiert.

### `sqlite.py`

Stellt kontrollierte, parametrisierte SQLite-Operationen bereit.

Unterstützt unter anderem:

- Datenbankverbindungen
- Schema-Initialisierung
- einzelne Statements
- Batch-Statements
- `fetch_one` und `fetch_all`
- Transaktionen mit Commit und Rollback
- Tabellenprüfung
- `VACUUM`
- Datenbank-Backups

SQL-Parameter sollen grundsätzlich gebunden übergeben werden. Das verhindert SQL-Injection und trennt Daten von SQL-Befehlen.

## Sicherheitsprinzip

Alle Tools werden über ein gemeinsames Interface beschrieben und können vor der Ausführung zentral validiert werden. Berechtigungen, Dry-Run-Verhalten und potenziell destruktive oder netzwerkbasierte Aktionen müssen vor der tatsächlichen Ausführung geprüft werden.
