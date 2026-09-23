# Resume TODO

Offene beziehungsweise ausdrücklich vertagte Punkte aus der Session.

## Orchestrator und Workflow

- [x] Checkpoint-Infrastruktur mit `CheckpointStore`, Migration, Context-Integration und `Orchestrator.resume()` ergänzen.
- [x] Gespeicherten `ExecutionPlan` für den Retry-Resume rekonstruieren und bereits erledigte Schritte überspringen.
- [ ] Resume für alle Checkpoint-Aktionen vollständig umsetzen. Aktuell wird ein gespeicherter Plan nur bei `retry_execution` direkt weiterverwendet; `wait`/`validate` fallen noch auf den normalen Replan-Pfad zurück.
- [ ] Resume-Strategie abhängig vom Waiting-Grund implementieren: Freigabe, externe Information, temporärer Fehler oder Replanning.
- [ ] Resume-Idempotenz atomar sicherstellen; `mark_resumed()` und `task.resumed` sind aktuell getrennte Schreibvorgänge.
- [ ] Vor dem Resume Taskstatus, Planversion, Workspace und externe Voraussetzungen validieren.
- [x] Unerwartete Stage-Exceptions werden im Orchestrator grundsätzlich in Stage-Fehler und `task.failed` überführt; weitere Fehlertransaktions-Integration bleibt zu testen.
- [x] Ungültige `next_action`-Werte werden strukturiert klassifiziert und als fehlgeschlagen persistiert.

## Persistenz und Transaktionen

- [ ] Die `EngineUnitOfWork` konsequent für alle Workflow-Entscheidungen verwenden: `retry_execution`, `replan`, `wait`, `stop` und `failed`. Cycle-Start und Execution-Persistierung sind bereits integriert.
- [ ] Waiting-Checkpoint, Taskstatus, Attempt und `task.waiting` in einem gemeinsamen atomaren Block persistieren.
- [ ] Cycle, Attempt, Planversion und Events relational beziehungsweise eindeutig miteinander verknüpfen.
- [ ] Idempotente Artefaktregistrierung mit Deduplizierung und optionaler Checksum-Prüfung ergänzen.
- [ ] `changed_files` fachlich entscheiden: als eigene Artefakte persistieren oder als separates Ergebnisobjekt speichern.
- [ ] Fallback und Diagnose verbessern, wenn auch die Fehlertransaktion selbst fehlschlägt.

## Konfiguration und Verdrahtung

- [ ] Eine zentrale Application-/Engine-Factory ergänzen, die Config, Datenbank, `StoreBundle`, `ContextBuilder` und `Orchestrator` automatisch verdrahtet.
- [ ] Sicherstellen, dass `execution.persistence_mode` aus `config.yaml` ohne manuelle Weitergabe im Orchestrator ankommt; die Config und StoreFactory sind vorhanden, die Application-Verdrahtung fehlt noch.
- [ ] Für `optional` fehlende Stores sichtbar als Warnung oder Diagnose melden.

## Human in the Middle

- [ ] Die HITM-Regeln aus dem Vault technisch implementieren: Approval-Request, Approval-Gate, Scope-Bindung und persistierte Entscheidung.
- [ ] Freigaben an Task, Planversion, Step, Tool und Berechtigungsscope binden.
- [ ] Freigaben bei Replanning, Toolwechsel oder Scope-Erweiterung automatisch invalidieren.
- [ ] Approval-Events und Resume nach Freigabe in den Orchestrator integrieren.

## Secrets

- [ ] Die im Vault dokumentierte macOS-Keychain-Nutzung technisch implementieren beziehungsweise den bestehenden Secret-Provider daran anbinden.
- [ ] Service-/Account-Mapping für Keychain-Secrets definieren.
- [ ] Tests für fehlende, verweigerte und erfolgreiche Keychain-Zugriffe ergänzen, ohne Secret-Werte zu loggen oder zu persistieren.

## Qualität

- [ ] Nach den nächsten Änderungen die vollständige Suite und 100-%-Coverage erneut ausführen.
- [ ] Die aktuell nicht committeten Vault-/Repository-Änderungen prüfen, committen und bei Bedarf pushen.
