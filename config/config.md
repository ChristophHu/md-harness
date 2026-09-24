# Konfiguration

## Dateien

- `config.yaml`: zentrale Anwendungskonfiguration
- `schemas/`: optionale Schemata zur strukturellen Validierung

## Ausführung

Der Abschnitt `execution` steuert den Engine-Lauf:

```yaml
execution:
  dry_run: true
  max_retries: 2
  max_cycles: 3
  persistence_mode: required
  hitl:
    mode: minimal
```

### `PersistenceMode`

`PersistenceMode` legt fest, ob und in welchem Umfang der Orchestrator seinen Lauf in SQLite persistieren muss. Betroffen sind insbesondere Taskstatus, Attempts, Events, Zeitstempel und Artefakte.

#### `required`

Produktionsmodus. `TaskStore`, `EventStore` und `ArtifactStore` müssen beim Erzeugen des Orchestrators vorhanden sein. Fehlt einer der Stores, wird die Konfiguration abgelehnt und der Lauf startet nicht.

Dieser Modus ist für reguläre produktive Ausführungen empfohlen, weil dadurch kein stiller Verlust von Status- oder Auditdaten möglich ist.

#### `optional`

Kompatibilitäts- und Entwicklungsmodus. Fehlende Stores werden toleriert. Der Lauf kann ohne vollständige Persistenz ausgeführt werden; entsprechend können Statusänderungen, Events, Attempts und Artefakte fehlen.

Dieser Modus ist aktuell der Default des Python-Modells und eignet sich für isolierte Tests oder schrittweise Integration.

#### `disabled`

Bewusst persistenzfreier Modus. Der Orchestrator verwendet keine Persistenz. Dieser Modus ist für reine In-Memory-Tests oder lokale Experimente gedacht und sollte nicht für produktive Tasks verwendet werden.

### Weitere Ausführungseinstellungen

- `dry_run`: verhindert je nach Toolrichtlinie schreibende oder destruktive Operationen.
- `max_retries`: maximale Anzahl erneuter Ausführungen nach `retry_execution`.
- `max_cycles`: maximale Anzahl von Planungs-, Ausführungs- und Validierungszyklen.
- `hitl.mode`: `minimal` (Default, rückwärtskompatibel), `selective` oder `interactive`. Der Modus steuert optionale Planreviews und menschliche Rückfragen. Er schaltet weder verpflichtende Tool-Approvals noch statische Sicherheitsverbote ab.

## Empfehlung

Für produktive Konfigurationen:

```yaml
execution:
  persistence_mode: required
```

Für Unit-Tests kann `disabled` oder `optional` verwendet werden. Die Anwendung muss den konfigurierten Wert beim Erzeugen des `Orchestrator` als `persistence_mode` weitergeben.
