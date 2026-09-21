# workflows

Fachliche und technische Abläufe des Harnesses. Die Workflows beschreiben, wie Aufgaben geplant, umgesetzt, geprüft und abgeschlossen werden.

## Inhalt

- `task-loop`: allgemeiner Aufgabenzyklus
- `feature`: Entwicklung neuer Funktionen
- `bugfix`: Analyse und Behebung von Fehlern
- `refactoring`: strukturelle Verbesserungen
- `review`: Prüfung von Änderungen
- `quality-check`: geplanter Qualitätsworkflow

## Geplante Qualitätsprüfungen

Im weiteren Verlauf werden folgende Werkzeuge in den Qualitätsworkflow integriert:

- **Ruff**: Linting und Formatierungsprüfung für Python
- **SQLFluff**: Syntax-, Stil- und Formatierungsprüfung für SQL
- **mypy**: statische Typprüfung für Python

Die Prüfungen sollen vor Review, Commit und Integration ausgeführt werden. Fehler werden dokumentiert, behoben und anschließend durch Tests verifiziert.
