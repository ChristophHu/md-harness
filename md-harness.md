

# Harness

## Projekt- und Workspace-Modell

Der Repository-Root ist das vollständige Harness-Projekt. Er enthält sowohl den Python-Quellcode als auch Konfiguration, Markdown-Instruktionen, Workflows, Agentendefinitionen, Knowledge-Dateien, Tests, Templates und Laufzeitdaten.

Der Ordner `src/harness/` enthält ausschließlich die ausführbare Python-Implementierung des Harnesses. Die Entwicklung des Harnesses selbst verteilt sich deshalb auf den gesamten Repository-Root: Python-Code wird unter `src/` gepflegt, fachliche Regeln und Spezifikationen in den übrigen Projektordnern.

Das Entwicklungsprojekt, das später durch das Harness bearbeitet wird, sollte in einem separaten Workspace liegen. Dadurch bleiben Harness-Code, Harness-Konfiguration und Zielprojekt getrennt.

Beispiel:

```text
/Users/.../md-harness/              ← Harness selbst
/Users/.../developer-projects/app/  ← vom Harness bearbeitetes Zielprojekt
```

Der Zielpfad wird über die Konfiguration festgelegt, beispielsweise:

```yaml
project:
  workspace_dir: /Users/.../developer-projects/app
```

Das Harness-Repository ist somit nicht automatisch der Arbeitsbereich des Zielprojekts.

## Markdown-Frontmatter

Frontmatter ist ein strukturierter Metadatenblock am Anfang einer Markdown-Datei. Er steht zwischen zwei `---`-Zeilen und verwendet YAML:

```markdown
---
id: developer-agent
type: agent
version: 1
enabled: true
---
```

Frontmatter ermöglicht es dem Harness, Markdown-Dateien nicht nur als Text, sondern auch anhand ihrer Eigenschaften zu verarbeiten. Über `type` kann beispielsweise zwischen Agent, Workflow, Aufgabe und Architekturentscheidung unterschieden werden.

Typische Felder sind:

- `id`: eindeutige Kennung
- `type`: Dokumenttyp
- `version`: Version der Spezifikation
- `enabled`: Aktivierungsstatus
- `status`: aktueller fachlicher Status
- `priority`: Priorität einer Aufgabe
- `depends_on`: Abhängigkeiten einer Aufgabe
- `tags`: thematische Zuordnung

Frontmatter ist nicht für jede Markdown-Datei zwingend erforderlich. Es wird für Dateien benötigt, die vom Harness maschinell geladen, interpretiert, indiziert oder ausgeführt werden:

```text
instructions/
workflows/
agents/
tasks/
knowledge/decisions/
knowledge/conventions/
```

Reine Dokumentationsdateien wie `README.md`, `md-harness.md`, `src/src.md` oder `tests/tests.md` können ohne Frontmatter auskommen.

Als Projektregel gilt:

> Jede Markdown-Datei, die vom Harness interpretiert, indiziert, geladen oder ausgeführt wird, benötigt Frontmatter. Reine Dokumentationsdateien benötigen es nicht zwingend.

Beispiele:

```markdown
---
id: feature-workflow
type: workflow
version: 1
enabled: true
---
```

```markdown
---
id: task-001
type: task
status: ready
priority: high
depends_on: []
---
```

Die erlaubten Frontmatter-Felder sollten zentral dokumentiert und validiert werden. Dadurch bleiben die Markdown-Spezifikationen einheitlich und zuverlässig maschinenlesbar. Frontmatter ist außerdem mit Obsidian kompatibel und kann dort für Suche, Filter und Ansichten verwendet werden.

## Obsidian-Vault und Wissensarchitektur

Das Harness verwendet den Obsidian-Vault zunächst als lesbare, menschlich gepflegte Markdown-Wissensquelle. Die Obsidian-Anwendung selbst muss dabei nicht laufen; der Vault wird als normaler Ordner mit Markdown-Dateien behandelt.

Die empfohlenen Grundordner sind:

```text
~/md-harness-vault/
├── README.md
├── rules/
├── decisions/
├── architecture/
├── workflows/
├── agents/
├── knowledge/
├── memory/
├── templates/
└── inbox/
```

Die Verantwortlichkeiten sind:

- `rules/`: verbindliche Regeln und Einschränkungen
- `decisions/`: Architektur- und Projektentscheidungen
- `architecture/`: Systemaufbau und Komponenten
- `workflows/`: fachliche Entwicklungsabläufe
- `agents/`: Rollen und Verhaltensregeln der Agenten
- `knowledge/`: längerfristiges Projekt- und Fachwissen
- `memory/`: verdichtete Erfahrungen und Erkenntnisse
- `templates/`: Vorlagen für Regeln, Entscheidungen und Aufgaben
- `inbox/`: neue, noch nicht einsortierte Informationen

Die Systeme haben getrennte Verantwortlichkeiten:

```text
Vault       = menschlich gepflegte Wissens- und Regelquelle
SQLite      = operative Wahrheit für Aufgaben und Prozesse
Qdrant      = semantischer Suchindex
```

In den Vault gehören Regeln, Entscheidungen, Architekturwissen, Workflows, Agentenbeschreibungen, Projekterfahrungen und verdichtete Gedächtnisinhalte. SQLite verwaltet dagegen Aufgabenstatus, Prioritäten, Abhängigkeiten, Ausführungsversuche und Ereignisse. Qdrant erhält ausgewählte, semantisch durchsuchbare Inhalte aus dem Vault, ersetzt aber niemals deren originale Markdown-Dateien.

Aufgabenbeschreibungen können zusätzlich im Vault unter `tasks/` liegen. Der operative Aufgabenstatus bleibt jedoch ausschließlich in SQLite maßgeblich.

Nicht in Qdrant gehören typischerweise Secrets, rohe Konfigurationswerte, Logs, kurzlebige Laufzeitdaten und binäre Artefakte.

### Übersichtsdateien und Verlinkungen

Jeder Ordner im Vault soll eine eigene Übersichtsdatei enthalten. Die Datei soll denselben Namen wie der Ordner tragen, beispielsweise `rules/rules.md` oder `decisions/decisions.md`. Sie beschreibt kurz die Bedeutung des Ordners und listet seine wesentlichen Dateien und Unterordner auf.

Zusätzlich sollen die oben genannten fachlichen Verlinkungen in Obsidian genutzt werden. Beziehungen zwischen Regeln, Workflows, Entscheidungen, Aufgaben, Agenten und Architektur sollen mit Wiki-Links wie `[[rules-testing]]` oder `[[decision-001-task-engine]]` dargestellt werden.

Die Verantwortlichkeiten bleiben getrennt:

- Ordnerstruktur: technische Klassifikation
- Frontmatter: maschinenlesbare Metadaten
- Übersichtsdateien: Orientierung und Navigation
- Obsidian-Links: fachliche Beziehungen zwischen Dokumenten

Das Harness darf nicht von Obsidian-Links abhängig sein. Für die maschinelle Verarbeitung bleiben Dateipfade und Frontmatter maßgeblich.

Der Vault-Pfad wird in der Harness-Konfiguration hinterlegt:

```yaml
knowledge:
  sources:
    - type: local_markdown
      name: obsidian-vault
      path: ~/md-harness-vault
      enabled: true
      read_only: true
```

## Tests und Coverage

Jede neue Implementierung und jede relevante Änderung an bestehendem Code ist durch passende automatisierte Tests zu begleiten. Eine Implementierung gilt erst dann als abgeschlossen, wenn die zugehörigen Tests erfolgreich ausgeführt wurden.

Für das Projekt wird eine Testcoverage von mindestens 90 % angestrebt. Die Coverage wird bei jedem normalen Testlauf automatisch mit `pytest-cov` erzeugt.

Der Standard-Testlauf lautet:

```bash
uv run pytest
```

Dabei werden:

- alle Tests unter `tests/` ausgeführt
- die Coverage für `src/harness` ermittelt
- nicht getestete Zeilen im Terminal angezeigt
- ein HTML-Bericht unter `htmlcov/index.html` erzeugt

Ein gezielter Testlauf für einzelne Dateien ist ebenfalls möglich:

```bash
uv run pytest tests/unit/test_secrets.py tests/unit/test_rotation.py
```

Nicht sinnvoll messbare Bereiche, beispielsweise reine Paketinitialisierung oder Integrationscode für externe Dienste, dürfen begründet ausgeschlossen werden. Die Begründung muss dokumentiert werden.

## Storage und SQLite

Die Storage-Schicht befindet sich unter `src/harness/storage/`. SQLite ist die operative Quelle für Projekte, Aufgaben, Status, Abhängigkeiten, Akzeptanzkriterien, Agentenversuche, Ereignisse und Artefakte. Die produktive Datenbank liegt unter:

```text
state/harness.sqlite
```

Die Struktur ist von der Python-Logik getrennt:

- `schema.sql`: vollständiges Initialschema mit Tabellen, Indizes und Triggern
- `migrations/`: versionierte SQL-Änderungen im Format `NNN_name.sql`
- `database.py`: Verbindungen, Migrationen, Transaktionen, Schema-Version, Healthcheck, Backup und Wiederherstellung
- `project_store.py`: Projektzugriff
- `task_store.py`: Aufgaben- und Statuslogik
- `event_store.py`: Ereignisprotokollierung
- `artifact_store.py`: Artefaktverwaltung

Die Store-Klassen kapseln SQL und stellen der Engine fachliche Funktionen bereit. Der Aufgabenstatus wird über zulässige Zustandsübergänge geprüft. Änderungen an `tasks` aktualisieren `updated_at` automatisch; häufige Abfragen werden durch SQLite-Indizes unterstützt.

Neue Datenbanken werden aus `schema.sql` aufgebaut. Anschließend werden offene Migrationen ausgeführt und in `PRAGMA user_version` versioniert. Bestehende Migrationen werden nicht verändert; jede Schemaänderung erhält eine neue Migrationsdatei.

Die Datenbank wird nicht als Wissensquelle verwendet. Der Obsidian-Vault enthält Regeln, Entscheidungen und Dokumentation; SQLite enthält den operativen Prozesszustand. Reproduzierbare SQLite-Testdaten liegen unter `tests/fixtures/sqlite_seed.sql`.

Für die Storage-Schicht gilt ebenfalls die Testregel: Jede Änderung wird durch Tests begleitet. Der Standardlauf ist `uv run pytest`; die aktuelle Gesamt- und Storage-Coverage beträgt 100 %.

## Gitflow

Für die Entwicklung ist Gitflow verbindlich zu verwenden. Die Branches und ihre Rollen sind:

- `main` oder `master`: stabile, produktionsreife Versionen
- `develop`: Integrationsbasis für die nächste Version
- `feature/<name>`: einzelne Features, abgezweigt von `develop`
- `release/<version>`: Stabilisierung und Vorbereitung eines Releases
- `hotfix/<version>-<name>`: dringende Korrekturen ausgehend vom stabilen Branch

Die vorgesehenen Zusammenführungen sind:

```text
feature/*  → develop
release/*  → main/master
release/*  → develop
hotfix/*   → main/master
hotfix/*   → develop
```

Release- und Hotfix-Branches müssen nach dem Merge in den stabilen Branch auch nach `develop` zurückgeführt werden. Releases erhalten einen Versions-Tag wie `v1.0.0`. Der konkrete stabile Branch (`main` oder `master`) muss in der Projektkonfiguration eindeutig festgelegt sein.

## Codequalität und Fehlerbehebung

Codeanalyse, Codequalität, Refactoring und Fehlerbehebung werden als Workflow-Themen behandelt. Der Workflow `quality-check` prüft die aktuelle Python-Anwendung mit den dafür vorgesehenen Lintern:

- Ruff für Python-Code und Python-Formatierung
- SQLFluff für SQL-Dateien
- mypy für statische Typprüfung

Der Qualitätsworkflow umfasst Analyse, Fehlerbehebung, Tests und Coverage. Er soll vor Reviews, Commits und Integrationen erfolgreich durchlaufen werden.

## Konfiguration

## Struktur

### Dateien

#### Grundstruktur

```
harness/
├── pyproject.toml
├── README.md
├── .gitignore
├── .env.example
├── config/
│   ├── config.yaml
│   └── schemas/
│       └── config.schema.yaml
│
├── src/
│   └── harness/
│       ├── __main__.py
│       ├── cli.py
│       ├── config.py
│       ├── models.py
│       ├── engine/
│       │   ├── orchestrator.py
│       │   ├── planner.py
│       │   ├── executor.py
│       │   └── validator.py
│       ├── agents/
│       │   ├── registry.py
│       │   └── runner.py
│       ├── workflows/
│       │   ├── loader.py
│       │   └── runner.py
│       ├── knowledge/
│       │   ├── loader.py
│       │   ├── index.py
│       │   ├── memory.py
│       │   ├── qdrant.py
│       │   ├── retrieval.py
│       │   └── index.py
│       ├── tasks/
│       │   ├── repository.py
│       │   └── state_machine.py
│       ├── tools/
│       │   ├── base.py
│       │   ├── filesystem.py
│       │   ├── git.py
│       │   ├── kanban.py
│       │   └── sqlite.py
│       ├── storage/
│       │   ├── database.py
│       │   ├── sqlite_repository.py
│       │   └── migrations/
│       ├── integrations/
│       │   ├── obsidian.py
│       │   ├── git.py
│       │   └── kanban.py
│       └── security/
│           ├── permissions.py
│           └── command_policy.py
│
├── instructions/
├── workflows/
├── agents/
├── knowledge/
│   ├── loader.py
│   ├── index.py
│   ├── memory.py
│   └── retrieval.py
├── tasks/
├── templates/
│   ├── task.md
│   ├── decision.md
│   └── agent-result.md
├── state/
│   ├── harness.sqlite
│   └── runs/
├── logs/
├── artifacts/
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/
```



#### YAML-Frontmatter

##### Markdowns

Auch die Markdown-Dateien brauchen eine klare Priorität und ein maschinenlesbares Format. Freitext allein reicht nicht zuverlässig aus. Jede Datei sollte beispielsweise YAML-Frontmatter enthalten:

```
---
id: developer-agent
type: agent
version: 1
enabled: true
---

# Developer Agent

...
```



## Aufgaben

### SQLIte Datenbank





## Wissen (Knowledge)

| Komponente  | Hauptaufgabe                              | Rolle im Harness                                             |
| ----------- | ----------------------------------------- | ------------------------------------------------------------ |
| SQLite      | Strukturierte relationale Daten speichern | Aufgaben, Status, Abhängigkeiten, Ereignisse und Ergebnisse  |
| Obsidian    | Markdown-basiertes Wissensmanagement      | Menschlich lesbare Projektdokumentation und Wissensbasis     |
| `MEMORY.md` | Kurz- und Langzeitgedächtnis des Agenten  | Wichtige Erkenntnisse, Entscheidungen, Regeln und Erfahrungen |
| Qdrant      | Vektorbasierte semantische Suche          | Ähnliche Inhalte und relevante Wissenseinträge auffinden     |

### Obsidian



### `MEMORY.md`



### Qdrant
