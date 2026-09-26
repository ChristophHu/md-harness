# knowledge

## Inhalt

+ - `loader.py`: Markdown-Laden
 - `index.py`: Wissensindex

## Knowledge Loader

`loader.py` liest konfigurierte Markdown-Quellen schreibgeschützt und stellt relevante Dokumente für den `ContextBuilder` bereit.

Der Loader unterstützt im MVP lokale Markdown-Quellen (`local_markdown`), liest rekursiv `*.md`-Dateien, wertet YAML-Frontmatter aus und filtert nach Dokumenttyp, ID und Tags. Er liefert strukturierte `KnowledgeDocument`-Objekte, verhindert Zugriffe außerhalb der konfigurierten Quelle und verändert den Vault niemals.

### KnowledgeDocument

```text
KnowledgeDocument
├── path
├── content
├── document_id
├── document_type
├── tags
└── metadata
```

Frontmatter ist optional. Ungültiges Frontmatter wird als `KnowledgeError` gemeldet.

```yaml
knowledge:
  sources:
    - type: local_markdown
      name: obsidian-vault
      path: ~/md-harness-vault
      enabled: true
      read_only: true
```

`.env`, SQLite-Dateien, Logs, `state/` und `artifacts/` werden nicht als Knowledge geladen. Der Loader liest ausschließlich innerhalb des konfigurierten Quellpfads.
