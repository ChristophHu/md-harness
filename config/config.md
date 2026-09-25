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

### Ausführende Task-Agenten

`agents.enabled` aktiviert die profilgesteuerte Modellplanung für neue Tasks;
die Vorgabe ist `false`. Ein Profil enthält `name`, positive `version`,
`instructions`, `task_types`, eine Tool-Allowlist, `model` und ein positives
`max_steps`. Profile werden in Listenreihenfolge für nicht explizit zugewiesene
Tasks ausgewählt. Eine aktive `task_assignments`-Zuweisung übersteuert
`tasks.assigned_agent`. Die gewählte Profilversion wird pro Task festgehalten.

Bei aktivierter Schicht werden Aufgaben- und Vault-Inhalte an den konfigurierten
Modellanbieter gesendet. Der Schlüssel kommt aus `secrets.names.openai_api_key`
über Keychain bzw. Umgebung. Vor produktiver Aktivierung sollten ein
geeignetes Profil, ein zugängliches Modell, Datenschutzeignung und der
Approval-Prozess in `dry_run` geprüft werden. Effektbehaftete Agentenschritte
benötigen zusätzlich eine konkrete Step-Freigabe. Die Tool-Policy bleibt
verbindlich; ein Profil kann sie nicht abschwächen.

Technische Details und Wiederanlauf-Verhalten stehen in
`src/harness/agents/agents.md`.

## Empfehlung

Für produktive Konfigurationen:

```yaml
execution:
  persistence_mode: required
```

Für Unit-Tests kann `disabled` oder `optional` verwendet werden. Die Anwendung muss den konfigurierten Wert beim Erzeugen des `Orchestrator` als `persistence_mode` weitergeben.

## Unbeaufsichtigter Betrieb

Der Abschnitt `operations` konfiguriert Diagnose, Recovery, Backups und – auf
macOS – die ausdrücklich installierbaren LaunchAgents:

```yaml
operations:
  stale_task_minutes: 30
  outbox_pending_minutes: 15
  backup_directory: backups
  backup_keep: 7
  recovery_limit: 25
  auto_dispatch: false
  launchd_label: com.mdharness.operations
  maintenance_interval_seconds: 300
  backup_hour: 2
  backup_minute: 15
  alerts:
    provider: none
    webhook_secret: operations_slack_webhook
    state_file: ../state/alerts.json
    cooldown_minutes: 60
    escalation_minutes: 30
    repeat_minutes: 240
```

`service install` installiert Maintenance und Backup in der Login-Domain des
aktuellen macOS-Benutzers. Zeit-/Intervallwerte und Label werden vor dem
Schreiben validiert. Die Installation startet **keine** freigegebenen Tasks:
`auto_dispatch` bleibt unabhängig und standardmäßig `false`. Pro Datenbank und
Job verhindert ein Betriebssystem-Lock überlappende Scheduler-Läufe. Siehe
[`src/harness/storage/operations.md`](../src/harness/storage/operations.md)
für Installation, Status, Entfernung, Logs und Alarmierung. Slack wird erst bei
`operations.alerts.provider: slack` aktiviert; das Webhook-Secret muss im
Keychain oder über das zugeordnete Environment verfügbar sein.
