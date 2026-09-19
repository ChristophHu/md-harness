

# Harness

## Struktur

### Dateien

#### Grundstruktur

```
harness/
├── pyproject.toml
├── README.md
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
│       │   ├── kanban.py
│       │   └── qdrant.py
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

