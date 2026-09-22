---
id: mvp-workflow
type: workflow
version: 1
enabled: true
status: active
---

# MVP-Workflow

Der MVP-Workflow ist der kleinste vollständig durchlaufbare Aufgabenzyklus des MD Harness.
Er führt eine vorbereitete Aufgabe von `ready` bis `done` und speichert jeden Statusübergang als Ereignis in SQLite.

## Ablauf

```text
created → ready → planning → executing → validating → done
```

Bei einer fehlgeschlagenen Validierung wird der Fehler klassifiziert:

```text
validating → executing   (Ausführungsfehler)
validating → planning    (Planungsfehler)
validating → waiting     (Blockade)
```

## Schritte

### 1. Created und Ready

Die Aufgabe ist in SQLite angelegt (`created`) und enthält mindestens eine Beschreibung sowie Akzeptanzkriterien. Nach Prüfung wird sie auf `ready` gesetzt.

### 2. Planning

Der Planner erstellt einen konkreten Umsetzungsschrittplan. Relevanter Markdown-Kontext kann dabei geladen werden.

### 3. Executing

Der Executor setzt den Plan im konfigurierten Zielprojekt um.

### 4. Validating

Der Validator führt den verfügbaren automatisierten Testlauf aus und prüft die Akzeptanzkriterien.

### 5. Done

Nach erfolgreicher Validierung wird die Aufgabe auf `done` gesetzt. Ergebnis, Artefakte und Ereignisse bleiben nachvollziehbar gespeichert.

## Erforderliche MVP-Komponenten

- Task-Speicherung und Statusübergänge in SQLite
- Planner, Executor und Validator
- Ereignisprotokollierung für jeden Übergang
- automatisierter Testlauf

## Nicht Bestandteil des MVP

- parallele Aufgabenverarbeitung
- komplexe automatische Agentenauswahl
- Qdrant oder semantische Suche
- automatische Wissensverdichtung
- umfangreicher Review- und Release-Prozess

## Beispiel

Für die Aufgabe „Healthcheck für SQLite ergänzen“ erstellt der Planner einen Plan, der Executor ändert Code und Tests, und der Validator führt `uv run pytest` aus. Bei Erfolg wird die Aufgabe auf `done` gesetzt; bei Fehlern wird sie zur Überarbeitung an `executing` zurückgegeben.
