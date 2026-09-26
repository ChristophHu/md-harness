---
id: planner-convention
type: convention
version: 1
enabled: true
---

# Planner-Regeln

## Zweck

Der Planner erzeugt aus einem vollständigen `ExecutionContext` einen strukturierten `ExecutionPlan`. Der Plan ist die verbindliche Übergabe an den Executor.

## Muss-Regeln

- Der Planner darf keine Änderungen am Zielprojekt durchführen.
- Der Planner darf keine Tools ausführen.
- Der Planner darf keine Tests starten.
- Der Planner darf keine SQLite-Status ändern.
- Der Planner darf Akzeptanzkriterien nicht als erfüllt markieren.
- Der Planner muss einen vollständigen, überprüfbaren `ExecutionPlan` erzeugen.
- Der Planner muss fehlende, widersprüchliche oder unzureichende Informationen strukturiert melden.

## Erlaubte Aufgaben

- Task und Context analysieren
- Ziele und Akzeptanzkriterien in Schritte übersetzen
- Testkriterien den Schritten zuordnen
- benötigte Tools identifizieren
- Reihenfolge und Abhängigkeiten der Schritte bestimmen
- Annahmen und Risiken dokumentieren

## Ergebnisstatus

Wenn die Voraussetzungen nicht erfüllt sind, darf der Planner keinen unvollständigen Plan erzeugen:

```text
waiting → Freigabe oder externe Information fehlt
failed  → Kontext ist ungültig oder widersprüchlich
success → Plan konnte erstellt werden
```

Bei `success` ist ein vollständiger `ExecutionPlan` mit `goal`, `steps`, `assumptions` und `risks` zurückzugeben.

## Planmodell

Das Planmodell liegt in einer eigenen Engine-Datei:

```text
src/harness/engine/plan.py
```
