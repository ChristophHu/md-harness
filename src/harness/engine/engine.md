# engine

## Inhalt

- `orchestrator.py`: steuert den vollständigen Aufgabenzyklus und verbindet die Engine-Komponenten.
- `planner.py`: erstellt aus Aufgabe und Kontext einen ausführbaren Plan.
- `plan.py`: enthält die strukturierten Modelle `ExecutionPlan` und `PlanStep` als Ergebnis des Planners.
- `executor.py`: führt den Plan über die registrierten Tools im Zielprojekt aus.
- `executor.py`: führt ausschließlich die im Plan autorisierten Schritte und Tools aus.
- `validator.py`: prüft Ausführung, Tests und Akzeptanzkriterien.
- `context.py`: definiert den gemeinsamen Arbeitskontext für Planung, Ausführung und Validierung.
- `context_builder.py`: lädt Task-, Storage-, Knowledge- und Tool-Daten und erstellt daraus einen vollständigen `ExecutionContext`.

## Orchestrator

Der `Orchestrator` ist der zentrale Ablaufkoordinator der Engine. Er entscheidet nicht selbst, wie eine Aufgabe geplant, ausgeführt oder validiert wird. Seine Aufgabe ist es, die einzelnen Komponenten in der richtigen Reihenfolge mit demselben konsistenten Context zu verbinden.

Seine wesentlichen Aufgaben sind:

- einen vollständigen `ExecutionContext` über den `ContextBuilder` erzeugen
- sicherstellen, dass Planner, Executor und Validator vorhanden sind
- den Context an den Planner übergeben
- den erzeugten Plan an den Executor weiterreichen
- Ausführungsergebnis, Plan und Context an den Validator übergeben
- das finale `EngineResult` an den aufrufenden Prozess zurückgeben
- unvollständige Konfiguration als `waiting` statt als fehlerhafte Ausführung melden

Der Ablauf einer vollständigen Ausführung lautet:

```text
Orchestrator
    ↓
ContextBuilder → ExecutionContext
    ↓
Planner.plan(context) → Plan
    ↓
Executor.execute(context, plan) → Execution
    ↓
Validator.validate(context, plan, execution) → EngineResult
```

Der Orchestrator ist damit für die Prozesssteuerung zuständig, nicht für:

- SQL-Abfragen oder dauerhafte Speicherung
- Laden von Markdown-Wissen
- Auswahl oder direkte Ausführung einzelner Tools
- Erstellung konkreter Planungsschritte
- Änderungen am Zielprojekt
- fachliche Bewertung von Akzeptanzkriterien

Diese Verantwortlichkeiten bleiben bei `ContextBuilder`, Storage- und Knowledge-Komponenten, ToolRegistry, Planner, Executor beziehungsweise Validator. Die Komponenten werden per Dependency Injection eingebunden, damit sie unabhängig getestet und später ausgetauscht werden können.

### Zyklus- und Retry-Limits

Der Orchestrator verwendet zwei getrennte Grenzen aus der `execution`-Konfiguration:

```yaml
execution:
  max_retries: 2
  max_cycles: 3
```

`max_retries` begrenzt Wiederholungen der Ausführung desselben Plans. `max_cycles` begrenzt die gesamte Anzahl von Planner-/Executor-/Validator-Durchläufen und schützt vor Endlosschleifen durch wiederholtes Replanning. Beide Werte werden aus der normalen YAML-Konfiguration geladen; `.env` ist dafür nicht vorgesehen.

Der Orchestrator verwaltet pro Zyklus zusätzlich einen Eintrag in `task_attempts`. Retries und Replans werden als fehlgeschlagene Attempts abgeschlossen; erfolgreiche Validierung, Waiting und endgültige Fehler schließen den Attempt mit dem jeweiligen Ergebnisstatus ab. Nicht behebbares Scheitern setzt den Taskstatus auf `failed` und erzeugt ein `task.failed`-Event.

### Replanning-Feedback und Events

Bei einem Replan werden die Ergebnisse des Executors und Validators als JSON-Payload in `task_events` gespeichert. Verwendete Eventtypen sind:

```text
task.plan.created
task.execution.completed
task.validation.completed
task.replanning
```

`task.replanning` enthält den Grund, die vorherige und die nächste Planversion, Validierungsdetails und Fehler. Der `ContextBuilder` liest diese Events beim nächsten Lauf und ergänzt den `ExecutionContext` um:

- `last_plan`
- `last_validation`
- `replanning_reasons`

Der Planner kann dadurch den vorherigen Plan und das konkrete Validator-Feedback berücksichtigen. `ExecutionPlan` führt dafür `version`, `replanned_from` und `reason`.

## Planner

Der Planner erzeugt aus dem vollständigen `ExecutionContext` einen strukturierten `ExecutionPlan`. Er verändert das Zielprojekt nicht und führt keine Tools aus. Der Plan wird anschließend vom Executor verarbeitet.

### Aufgaben des Planners

- Task und Context analysieren
- Ziel und Akzeptanzkriterien in konkrete Schritte übersetzen
- Testkriterien den passenden Schritten zuordnen
- benötigte Tools identifizieren
- Reihenfolge und Abhängigkeiten der Schritte bestimmen
- Annahmen und Risiken dokumentieren
- fehlende oder widersprüchliche Informationen strukturiert melden

### Planungsschritte

```text
1. Context validieren
2. Ziel aus dem Task bestimmen
3. Akzeptanzkriterien in Planziele übersetzen
4. relevante Testkriterien zuordnen
5. Schritte in eine ausführbare Reihenfolge bringen
6. benötigte Tools prüfen
7. Risiken und Annahmen dokumentieren
8. ExecutionPlan zurückgeben
```

Der Planner wählt für die Implementierungs- und Validierungsschritte konkrete Tools aus `available_tools`. Für die Implementierung werden bevorzugt `filesystem` oder `git`, für die Validierung `sqlite` oder `filesystem` verwendet. Ist kein passendes Tool verfügbar, wird kein unvollständiger Plan erzeugt, sondern ein fehlgeschlagenes Ergebnis mit dem entsprechenden Fehlercode zurückgegeben.

Wenn die Voraussetzungen nicht erfüllt sind, erzeugt der Planner keinen unvollständigen Plan. Er liefert stattdessen ein strukturiertes Ergebnis:

```text
waiting → Freigabe oder externe Information fehlt
failed  → Kontext ist ungültig oder widersprüchlich
success → Plan konnte erstellt werden
```

Der Planner darf analysieren, strukturieren, zuordnen und Vorschläge machen. Er darf keine Dateien schreiben, keine Tools ausführen, keine Tests starten, keine SQLite-Status ändern und keine Akzeptanzkriterien als erfüllt markieren.

Das Ergebnis des Planners ist ein `ExecutionPlan` aus `src/harness/engine/plan.py`:

```text
ExecutionPlan
├── goal
├── steps
├── assumptions
└── risks
```

Ein `PlanStep` enthält mindestens eine Kennung, eine Beschreibung, eine Aktion und optional das vorgesehene Tool, Argumente sowie die zugeordneten Acceptance- und Testkriterien. `ExecutionPlan` und `PlanStep` werden bewusst in einer eigenen Datei gehalten, damit Planner und Executor ein gemeinsames, stabiles Datenmodell verwenden.

## ToolRegistry

Die zentrale ToolRegistry ist bereits unter `src/harness/tools/base.py` implementiert. Sie verwaltet die für einen Agenten verfügbaren Tools und stellt deren standardisierte Definitionen bereit.

Die Registry wird zentral über `src/harness/tools/factory.py` und `ToolRegistryFactory` aufgebaut. Die Factory konfiguriert Workspace, Git-Repository, SQLite-Datenbank und aktivierte Tools konsistent. Der Orchestrator nimmt eine solche Registry über `tool_registry` entgegen und verdrahtet dieselbe Instanz mit `ContextBuilder` und `Executor`. Unterschiedliche Registry-Instanzen werden abgelehnt; ein explizit injizierter Executor bleibt für Spezialfälle und Tests möglich.

Die `ToolSecurityPolicy` prüft zusätzlich zu Planfreigabe und Toolverfügbarkeit:

- Permission-Level des Tools
- Netzwerkfreigaben
- destruktive Operationen
- erforderliche Task-Freigabe
- Grenzen des konfigurierten Workspace
- Verhalten bei `dry_run`

Erst wenn alle Prüfungen erfolgreich sind, darf der Executor das Tool aufrufen. Die Policy liegt unter `src/harness/security/tool_policy.py`.

Die Registry kann:

- Tools anhand ihres Namens registrieren
- doppelte Toolnamen erkennen
- registrierte Tools laden
- verfügbare Tool-Definitionen auflisten
- Tool-Argumente validieren und Tools ausführen

Bereits vorhandene Tools sind:

- `FilesystemTool` für kontrollierte Dateioperationen
- `GitRepository` für kontrollierte Git-Operationen
- `SQLiteTool` für parametrisierte SQLite-Zugriffe

Der `ContextBuilder` liest über `tool_registry.list()` die verfügbaren Toolnamen und übernimmt sie in den `ExecutionContext`. Die Registry führt selbst keine automatische Auswahl durch; sie stellt nur die registrierten und berechtigten Tools bereit.

Die zentrale Initialisierung erfolgt über `src/harness/application.py`. `build_orchestrator(...)` lädt die YAML-Konfiguration, initialisiert Datenbank und `StoreBundle`, erzeugt die `ToolRegistry` über `ToolRegistryFactory`, konfiguriert den `ContextBuilder` und verdrahtet anschließend Planner, Executor, Validator und Orchestrator. Dabei werden `dry_run`, `max_retries`, `max_cycles` und `persistence_mode` aus `ExecutionConfig` übernommen.

Der Anwendungseinstieg liegt in `src/harness/cli.py`. Ein produktiver Lauf kann über die folgenden Befehle gestartet oder gesteuert werden:

```text
harness --config config/config.yaml run <task-id>
harness --config config/config.yaml resume <task-id>
harness --config config/config.yaml cancel <task-id>
```

Die CLI hält keine eigene Engine- oder Persistenzlogik, sondern verwendet ausschließlich die Composition Root. `persistence_mode: disabled` ist für den produktiven CLI-Bootstrap absichtlich nicht zugelassen, weil der CLI-Lauf auf der SQLite-Aufgabenverwaltung basiert.
- `result.py`: definiert standardisierte Ergebnisse, Statuswerte, Fehler, Tests und Artefakte.

## Executor und Tool-Sicherheit

Der Executor erhält den `ExecutionContext`, den `ExecutionPlan`, eine injizierte `ToolRegistry` und eine `ToolSecurityPolicy`. Die Registry enthält alle registrierten Tools, stellt aber keine pauschale Ausführungsfreigabe dar.

Für jeden Plan-Schritt gilt:

1. Der Executor löst das im Schritt angegebene Tool aus der Registry auf.
2. Die `ToolSecurityPolicy` prüft, ob das Tool exakt im Plan-Schritt autorisiert ist.
3. Das Tool muss im `ExecutionContext` als verfügbar aufgeführt sein.
4. Die ToolRegistry validiert die Argumente.
5. Erst danach wird das Tool ausgeführt.

Tools, die zwar registriert sind, aber nicht im Plan stehen, dürfen nicht verwendet werden. Die Registry wird nicht in den serialisierbaren `ExecutionContext` gelegt, sondern als Laufzeitabhängigkeit direkt in den Executor injiziert.

Fehlt ein Tool, ist es nicht verfügbar oder verletzt die Policy, wird nicht auf ein anderes Tool ausgewichen. Der Executor liefert ein fehlgeschlagenes Ergebnis mit `next_action = replan`. Der Orchestrator kann den Ablauf damit zurück an den Planner geben. Ein gewöhnlicher Fehler während der Umsetzung bleibt dagegen ein Ausführungsfehler und führt zurück zu `executing`.

## ExecutionResult

`EngineResult` beschreibt den äußeren Workflow-Status. Die konkreten Ausführungsdetails werden durch `ExecutionResult` und `StepExecution` in `result.py` modelliert:

```text
ExecutionResult
├── status
├── steps
├── changed_files
├── artifacts
├── errors
└── next_action
```

Jeder `StepExecution` enthält Schritt-ID, Status, verwendetes Tool, Tool-Argumente, normalisiertes Tool-Ergebnis, Artefakte, geänderte Dateien, Zeitstempel und einen möglichen Fehler. `ToolExecutionResult` kapselt Tool-Ausgabe, strukturierte Daten, Exit-Code und Fehler sicher in einem einheitlichen Format.

`ExecutionResult` enthält zusätzlich Attempt-ID, Dry-Run-Information und Laufzeitstempel. `NextAction` typisiert die empfohlene Folgeaktion:

```text
validate        → an den Validator weitergeben
retry_execution → Ausführung erneut versuchen
replan          → zum Planner zurückkehren
wait            → auf externe Voraussetzung warten
stop            → Lauf beenden
```

Die möglichen Schrittstatus sind `success`, `failed`, `waiting` und `skipped`.

Die relevanten Executor-Ergebnisse sind:

```text
success  → Ausführung an den Validator weitergeben
waiting  → fehlende Workspace- oder externe Voraussetzung
failed   → Fehlerdetails zurückgeben; bei Tool-/Policy-Problem `next_action = replan`
```

## Verantwortungsgrenzen

Die Engine koordiniert den Ablauf, besitzt aber nicht die zugrunde liegenden Datenquellen:

- Tasks und Status werden über `src/harness/tasks/` und `src/harness/storage/` verwaltet.
- Markdown-Wissen wird über `src/harness/knowledge/` geladen.
- Aktionen werden über `src/harness/tools/` ausgeführt.
- Sicherheits- und Freigaberegeln liegen unter `src/harness/security/`.

`ExecutionContext` bündelt die für einen Lauf benötigten Daten. Er ist ein Laufzeitobjekt und ersetzt weder SQLite noch den Markdown-Vault.

## ExecutionContext und SQLite

### Fehlerklassifikation

Ausführungsfehler werden als `ExecutionError` mit einem stabilen `ExecutionErrorType`, optionalem Tool- und Step-Bezug sowie Retry- und Detailinformationen zurückgegeben. Die zentrale Klassifikation leitet daraus die nächste Aktion ab: Tool- und Policy-Fehler führen zu `replan`, temporäre Tool- oder Workspace-Fehler zu `retry_execution`, externe Blocker zu `wait` und eine fehlerfreie Ausführung zu `validate`. Workspace-Fehler mit erwarteter Berechtigungsfreigabe werden ebenfalls in `wait` überführt; fehlende oder ungültige Workspaces führen zu `replan`.

Der Orchestrator wertet `ExecutionResult.next_action` nach jeder Ausführung aus. `validate` ruft den Validator auf, `retry_execution` wiederholt die Ausführung mit demselben Plan, `replan` startet die Planung mit aktualisiertem Kontext erneut, `wait` beendet den Lauf im Status `waiting` und `stop` beendet ihn erfolgreich im Status `done`. Jede Entscheidung wird als `task.execution.next_action` persistiert.

Unbekannte `next_action`-Werte werden als `invalid_next_action` klassifiziert. Der aktuelle Attempt und Task werden auf `failed` gesetzt; zusätzlich werden `task.execution.invalid_next_action` beziehungsweise `task.validation.invalid_next_action` sowie `task.failed` persistiert.

Ein `ContextBuilder` sollte mit `ContextBuilder.from_stores(...)` aus demselben `StoreBundle` wie der Orchestrator erzeugt werden. Dadurch werden `TaskStore`, `EventStore`, `ArtifactStore` und `CheckpointStore` automatisch gemeinsam verdrahtet und auf Identität geprüft.

Die Persistenz wird über `execution.persistence_mode` konfiguriert:

- `required`: `TaskStore`, `EventStore` und `ArtifactStore` müssen vorhanden sein; andernfalls wird die Orchestrator-Konfiguration abgelehnt.
- `optional`: fehlende Stores werden toleriert, die Ausführung kann ohne vollständige Persistenz laufen.
- `disabled`: persistenzfreier Lauf, insbesondere für isolierte Tests.

### Atomare Workflow-Phasen

Die Persistenz wird fachlich in Phasen gebündelt:

1. Cycle-Start: Taskstatus, neuer Attempt und Start-Event
2. Execution-Ergebnis: Execution-Event, Artefakte und Attempt-Abschluss
3. Workflow-Entscheidung: nächster Status und Folge-Event

Jede Phase wird durch `EngineUnitOfWork` atomar committed oder vollständig zurückgerollt. Die Toolausführung selbst liegt außerhalb der SQLite-Transaktion. Bei einem Persistenzfehler wird eine separate Fehlertransaktion verwendet, die `task.persistence.failed` und den Status `failed` speichert.

SQLite enthält alle fachlich relevanten Informationen für einen Task und seine Abnahme. Die Daten sind auf mehrere Tabellen verteilt und werden für einen Engine-Lauf zu einem `ExecutionContext` zusammengeführt.

Die Tabelle `tasks` liefert die Kerndaten:

```text
id, project_id, parent_id, external_key, title, description,
task_type, status, priority, approval_status, assigned_agent,
created_at, updated_at, started_at, planning_started_at,
validation_started_at, failed_at, completed_at
```

Die Abnahme und technische Validierung werden getrennt modelliert:

- `task_acceptance_criteria`: fachliche Kriterien mit `id`, Beschreibung und `completed`-Status
- `task_test_criteria`: technische Prüfungen mit Kriterium, Testtyp, Kommando und `completed`-Status

Weitere Bestandteile des Laufzeitkontexts kommen aus:

- `task_dependencies`: blockierende und sonstige Abhängigkeiten
- `task_attempts`: bisherige Ausführungsversuche und Fehler
- `task_events`: Ereignishistorie des Tasks
- `task_artifacts`: erzeugte Dateien und Ergebnisse
- `task_approvals`: Freigabehistorie
- `projects`: Zielprojekt und Workspace-Pfad
- `task_assignments`: Agentenzuweisungen

Der Context enthält damit neben der Aufgabenbeschreibung auch Status, Freigabe, Akzeptanzkriterien, Testkriterien, Abhängigkeiten, Historie und Artefakte. `knowledge_documents` werden aus dem Markdown-Vault geladen, `available_tools` aus der `ToolRegistry` und `dry_run` aus der Konfiguration.

Für die vollständige Abbildung sollten Akzeptanz- und Testkriterien als strukturierte Objekte und nicht nur als einfache Strings in den Context übernommen werden, damit IDs, Testkommandos und Erfüllungsstatus erhalten bleiben.

Ein späterer `ContextBuilder` lädt die Daten über `TaskStore`, `EventStore`, `ArtifactStore`, `ProjectStore`, Knowledge Loader und `ToolRegistry` und erstellt daraus eine konsistente Momentaufnahme. `context.py` selbst führt keine SQL-Abfragen aus und speichert keine Daten dauerhaft.

Der Context wird über strukturierte Modelle für `AcceptanceCriterion` und `TestCriterion` aufgebaut. Dadurch bleiben IDs, Erfüllungsstatus, Testtyp und Testkommando erhalten. Zusätzlich enthält er `task_type`, `priority`, `task_status`, `approval_status`, `assigned_agent`, `dependencies`, `previous_attempts` und `artifacts`.

Dependencies werden als strukturierte `DependencyContext`-Objekte übernommen:

```text
DependencyContext
├── task_id
├── status
├── dependency_type
└── resolved
```

Der `ContextBuilder` lädt den aktuellen Status jeder abhängigen Aufgabe aus SQLite. Eine blockierende Dependency ist nur dann aufgelöst, wenn ihr Status `done` ist. Dadurch kann der Planner vor der Planerstellung erkennen, ob eine Aufgabe auf eine andere Aufgabe warten muss. Dafür ist keine zusätzliche SQLite-Struktur erforderlich: `task_dependencies` liefert die Beziehung und `tasks.status` den aktuellen Status.

Die geplante Integration lautet:

```text
TaskStore / EventStore / ArtifactStore / ProjectStore
Knowledge Loader / ToolRegistry / Config
                         ↓
                   ContextBuilder
                         ↓
                  ExecutionContext
                    ↙       ↓       ↘
                Planner  Executor  Validator
```

Der `ContextBuilder` ist die nächste Integrationskomponente. Er lädt die Daten aus den bestehenden Stores und Diensten, normalisiert sie in die Modelle aus `context.py` und übergibt eine konsistente Momentaufnahme an die Engine. Dadurch bleiben Planner, Executor und Validator frei von direkten SQL- und Dateisystemzugriffen.

`EngineResult` ist das gemeinsame Übergabeformat zwischen Planner, Executor, Validator und Orchestrator. Die Statuswerte sind `success`, `failed` und `waiting`.

## MVP-Ablauf

```text
created → ready → planning → executing → validating → done
```

## Validierung und Rücksprünge

Die Validierung entscheidet nicht nur über Erfolg oder Fehler, sondern klassifiziert auch die nächste Aktion:

```text
validating
├─ Erfolg       → done
├─ lokaler Fehler → executing
├─ Planfehler   → planning
└─ blockiert    → waiting
```

Ein lokaler Ausführungsfehler bedeutet, dass der bestehende Plan weiterhin gültig ist. Nur die Umsetzung muss korrigiert werden. Beispiele sind ein fehlschlagender Test, ein Formatierungsfehler oder ein nicht korrekt erzeugter Dateiinhalt.

Ein Planfehler bedeutet, dass die Akzeptanzkriterien mit dem bisherigen Plan nicht erfüllt werden können. Der Planner muss den Plan überarbeiten, bevor die Ausführung erneut beginnt.

Der Status `waiting` wird verwendet, wenn eine externe Voraussetzung oder eine menschliche Entscheidung fehlt, beispielsweise eine Freigabe, ein Zugang oder eine benötigte Information. Nach Auflösung der Blockade wird der Workflow an der passenden Stelle fortgesetzt.

Der vollständige Ablauf lautet damit:

```text
created
  → ready
  → planning
  → executing
  → validating
  ├─ Erfolg         → done
  ├─ Ausführungsfehler → executing
  ├─ Planfehler     → planning
  └─ Blockade         → waiting
```

Die SQLite-Aufgabe verwendet dieselben Workflow-Statuswerte wie die Engine: `created`, `ready`, `planning`, `executing`, `validating`, `waiting`, `done` und `cancelled`. Jeder Statuswechsel wird über `TaskStore` geprüft und kann als Event protokolliert werden.
