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

## ToolRegistryFactory

`factory.py` stellt mit `ToolRegistryFactory` eine zentrale Stelle zur Erzeugung einer konsistent konfigurierten `ToolRegistry` bereit.

Die Factory:

- registriert standardmäßig `filesystem`, `git` und `sqlite`
- beschränkt alle Tools auf den angegebenen Workspace beziehungsweise die angegebene Datenbank
- erlaubt ein explizites Teilset über `enabled_tools`
- lehnt unbekannte Toolnamen ab
- ermöglicht sichere Defaults für destruktive Dateioperationen
- erzeugt reproduzierbare Registries für Produktion und Tests

Beispiel:

```python
registry = ToolRegistryFactory.create(
    workspace="/workspace/project",
    database="/workspace/project/state/harness.sqlite",
    enabled_tools={"filesystem", "git"},
)
```

Die Factory erzeugt und konfiguriert Tools, führt sie aber nicht aus. Die Ausführung und die Prüfung, ob ein Tool im konkreten Plan autorisiert ist, bleiben beim Executor und bei `ToolSecurityPolicy`.

## Tool Security Policy

`security/tool_policy.py` autorisiert jeden Tool-Aufruf vor der Ausführung. Die Policy prüft:

- ob das Tool exakt im Plan-Schritt hinterlegt ist
- ob das Tool im Context verfügbar ist
- ob das Permission-Level (`read`, `write`, `destructive`, `network`) erlaubt ist
- ob Netzwerkzugriff freigegeben wurde
- ob destruktive Operationen freigegeben wurden
- ob der Task genehmigt ist
- ob Toolpfade innerhalb des Workspace liegen
- ob Schreib- oder Netzwerkoperationen im `dry_run` erlaubt sind

Ein registriertes Tool ist damit nicht automatisch ausführbar. Die Registry beschreibt die verfügbaren Werkzeuge, der Plan legt die erlaubten Werkzeuge für den konkreten Lauf fest und die Security Policy erzwingt diese Grenze.

### Vorteile

- einheitliche Tool-Konfiguration für alle Engine-Läufe
- zentrale Anwendung von Workspace- und Sicherheitsgrenzen
- weniger duplizierte Initialisierungslogik
- einfache Tests mit einem begrenzten Toolset
- spätere Erweiterbarkeit über Konfiguration

### Nachteile und Grenzen

- zusätzliche Abstraktionsschicht bei wenigen Tools
- fehlerhafte Defaults könnten zu einer zu weit gefassten Registry führen
- die Factory darf nicht zur versteckten Ausführungs- oder Autorisierungslogik werden

Der Orchestrator kann eine erzeugte Registry über `tool_registry` erhalten und daraus bei Bedarf den Executor aufbauen. Eine bereits injizierte Executor-Instanz hat Vorrang und bleibt für Spezialfälle und Tests möglich.
