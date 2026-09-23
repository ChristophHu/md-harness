# Offene Lücken

Stand: 2026-09-23. Diese Liste wurde gegen den aktuellen Orchestrator, die Persistenzschicht und Tests geprüft. Die zuletzt geschlossenen Resume-Themen
sind als erledigt markiert; offene Punkte beschreiben verbleibende Arbeit und keine bereits vorhandenen Features.

## Resume und Orchestrator

- [x] Validation-Resume über Validator, Attempt-Abschluss, Statusübergang,
  Events und Checkpoint-Entscheidung führen.
- [x] WAIT-Resume nach Approval-, temporärem Fehler-, Workspace-, manuellem
  Replan- und externem Informationsgrund unterscheiden.
- [x] Resume-Claim mit Token und Ablaufzeit einführen; abgelaufene Claims
  können übernommen werden. Claim und `task.resumed`-Event werden im UOW
  gemeinsam persistiert.
- [x] Erfolgreiche Validierung schließt Task, Attempt, Event und Checkpoint
  atomar ab; `resume()` auf einem `done`-Task führt keine Stages erneut aus.
- [x] Neue WAIT-Ergebnisse verlangen einen typisierten `WaitReason`; die
  Freitext-Heuristik dient nur dem Lesen alter Checkpoints.
- [x] Externe Information mit `wait_token`, atomarer Auflösung, Event und
  persistierter Informationsreferenz modellieren; der Context trägt die
  Referenz nach einem Neustart.
- [ ] Lease während langer Resume-Läufe verlängern oder mit Fencing absichern. Derzeit wird eine feste Lease vergeben und erst im `finally` freigegeben;
  überschreitet ein Lauf die Lease-Dauer, kann ein zweiter Worker übernehmen.
- [ ] Resume-Claim mit Taskstatus, Checkpoint-Version und Attempt-Zuordnung
  validieren. Die Checkpoint-Zeile wird nach dem Claim erneut gelesen; ein Lease-Claim
  allein prüft aber nicht, ob Taskstatus, Workspace oder gespeicherter Plan noch zusammenpassen.
- [ ] Cancellation gegen bereits laufende Resumer absichern. `cancel()` invalidiert den Checkpoint und leert dessen Lease, aber ein Worker, der den
  Checkpoint schon geladen hat, besitzt kein Fencing-Token für spätere Workflow-Schreibvorgänge.
- [x] Recovery nach einem echten Prozessabbruch direkt nach dem Resume-Claim
  und Neustart mit derselben SQLite-Datei testen.
- [ ] Weitere Crash-Phasengrenzen während Execution und laufender Validation
  testen. Der Commit der Validation-Entscheidung ist mit anschließendem
  Prozessabbruch und Neustart für Retry und Replan abgedeckt.

## Transaktionen und Workflow-Persistenz

- [ ] Sämtliche Workflow-Entscheidungen konsistent über `EngineUnitOfWork`
  persistieren. Execution-WAIT und Validation-Entscheidungen sind atomar;
  andere Execution-, Planungs- und Fehlerpfade nutzen teils noch einzelne
  Store-Schreibvorgänge oder Fallbacks.
- [x] Eine einheitliche Transaktionsgrenze für Validation-Ergebnis,
  `task.validation.completed`, Attempt-Abschluss, Statusentscheidung und
  Checkpoint-Änderung sicherstellen; Retry/Replan werden vor dem nächsten
  Zyklus resumierbar persistiert.
- [ ] Cycle, Attempt, Planversion und Events dauerhaft relational oder über
  stabile IDs verknüpfen; aktuell sind diese Zuordnungen teilweise nur in
  Checkpoint-Payloads/Events enthalten.
- [ ] Artefaktregistrierung idempotent mit Deduplizierung und optionaler
  Prüfsumme gestalten.
- [ ] Fachliche Behandlung von `changed_files` festlegen und persistieren
  (Artefakte oder separates Ergebnisobjekt).
- [ ] Fehlertransaktion und Diagnose absichern, falls auch die zweite
  Persistierung nach einem Workflow-Fehler fehlschlägt.

## Human in the Middle

- [ ] Approval-Request und Approval-Gate mit persistierter Entscheidung
  implementieren.
- [ ] Freigaben an Task, Planversion, Step, Tool und Berechtigungsscope binden.
- [ ] Freigaben bei Replanning, Toolwechsel oder Scope-Erweiterung
  invalidieren.
- [ ] Approval-Events sowie Freigabe- und Ablehnungsübergänge in den
  Orchestrator integrieren. Der aktuelle WAIT-Resume liest nur
  `approval_status` und ist noch kein vollständiger HITM-Workflow.

## Secrets und Betrieb

- [x] Den Secret-Provider an macOS Keychain anbinden und Service-/Account-Mapping festlegen.
- [x] Keychain-Zugriffe für fehlende, verweigerte und erfolgreiche Zugriffe
  testen; Secret-Werte werden nicht als Prozessargument oder Fehlermeldung
  ausgegeben.
- [ ] Für `optional` fehlende Stores eine sichtbare Diagnose bereitstellen.
- [ ] Migrations- und Resume-Recovery-Verhalten auf unterstützten Datenbank-/Betriebssystemkombinationen in CI abdecken.

## Qualität und Repository-Status

- [x] Unit-/Integrationstests für die aktuellen Änderungen ergänzen; lokaler
  Stand: 425 Tests, 100 % Coverage.
- [x] Coverage-Ziel in CI beibehalten und die Suite nach den Änderungen erneut ausführen.
