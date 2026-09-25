# MD Harness

Ein Python-Harness, das Markdown-Dateien als Spezifikation und SQLite als zentrale Aufgaben- und Zustandsdatenbank verwendet.

## Status

Der Orchestrator führt freigegebene Tasks über Planung, Tool-Ausführung und
Validierung. Läufe, Checkpoints, HITL-Anfragen und Ereignisse werden in SQLite
persistiert; die CLI unterstützt `run`, `resume`, `cancel` sowie Diagnose und
Backup-Wartung. Für Voraussetzungen, Konfiguration und Betriebsgrenzen siehe
[`src/harness/engine/engine.md`](src/harness/engine/engine.md) und
[`src/harness/storage/storage.md`](src/harness/storage/storage.md).

## Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m harness --help
```

## Betrieb

```bash
harness --config config/config.yaml doctor
harness --config config/config.yaml alert
harness --config config/config.yaml maintenance
harness --config config/config.yaml backup
harness --config config/config.yaml verify-backup /pfad/zum/backup.sqlite
harness --config config/config.yaml restore-backup /pfad/zum/backup.sqlite /neuer/pfad/recovered.sqlite
harness --config config/config.yaml service install
harness --config config/config.yaml service status
```

`doctor` gibt maschinenlesbares JSON aus und liefert bei Warnungen oder Fehlern
Exit-Code 1. Backups werden nach Erstellung in eine temporäre Datenbank
wiederhergestellt und geprüft. Regelmäßige Aufrufe und externe Alarmierung
werden über den Betriebssystem-Scheduler beziehungsweise dessen Monitoring
eingerichtet; Details stehen in
[`src/harness/storage/operations.md`](src/harness/storage/operations.md).
Der macOS LaunchAgent wird nur durch den expliziten `service install`-Befehl
aktiviert; `auto_dispatch` bleibt standardmäßig deaktiviert.
