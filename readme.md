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

## HTTP API und Swagger

Die optionale API stellt Task-Suche, Detailansicht und `run` bereit. Swagger UI
ist unter `/docs`, das OpenAPI-Schema unter `/openapi.json` verfügbar:

```bash
pip install -e '.[api]'
harness --config config/config.yaml serve
```

Host und Port werden im YAML unter `api` festgelegt:

```yaml
api:
  host: 127.0.0.1 # oder 0.0.0.0
  port: 3000
```

Standardmäßig lauscht der Server ausschließlich auf `127.0.0.1:8000`. CLI-Flags
`--host` und `--port` überschreiben die Konfiguration. Beim Binden an eine
andere Adresse muss vor dem Start `HARNESS_API_TOKEN` gesetzt sein; API-Aufrufe
müssen dann `Authorization: Bearer <token>` mitsenden.
Die synchronen Datenbank- und Orchestrator-Aufrufe werden innerhalb des
Prozesses serialisiert. Der `run`-Aufruf bleibt bis zum Abschluss des Laufs
offen; für einen öffentlich erreichbaren oder hochverfügbaren Betrieb sollte
ein vorgeschalteter authentifizierender Reverse Proxy verwendet werden.

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
