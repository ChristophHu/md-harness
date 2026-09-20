# harness

`src/harness/` ist das installierbare Python-Paket und damit die ausführbare Implementierung des Entwickler-Harnesses.

## Rolle

Das Paket stellt die technische Logik bereit, mit der Markdown-Spezifikationen gelesen, Workflows geplant, Aufgaben aus der SQLite-Datenbank verarbeitet, Agenten ausgeführt und Ergebnisse validiert werden.

## Abgrenzung

Der übergeordnete Repository-Root enthält das gesamte Harness-Projekt. Dazu gehören auch Konfiguration, Instruktionen, Markdown-Wissen, Templates, Tests und Laufzeitdaten. Diese fachlichen Grundlagen liegen bewusst nicht im Python-Paket.

Das vom Harness bearbeitete Entwicklungsprojekt ist ein separates Zielprojekt und gehört nicht automatisch in `src/harness/`.

## Inhalt

Enthalten sind CLI, Engine, Agenten, Workflows, Knowledge-Verarbeitung, Aufgabenverwaltung, Tools, Persistenz und Sicherheitslogik.
