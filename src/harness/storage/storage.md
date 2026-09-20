# storage

Die Storage-Schicht verwaltet die relationale Persistenz des Harnesses. SQLite ist die operative Quelle für Projekte, Aufgaben, Status, Abhängigkeiten, Ereignisse, Agentenversuche und Artefakte.

## Inhalt

- `database.py`: Verbindungen, Schema, Transaktionen, Healthcheck, Versionierung und Backup/Wiederherstellung
- `project_store.py`: Projekte anlegen und laden
- `task_store.py`: Aufgaben, Statuswechsel, Abhängigkeiten, Kriterien und Agentenversuche
- `event_store.py`: Aufgabenereignisse protokollieren
- `artifact_store.py`: erzeugte Artefakte registrieren
- `migrations/`: versionierte Weiterentwicklung des Schemas
- `__init__.py`: Storage-Paket

Die Store-Klassen kapseln SQL und geben der Engine eine fachliche Schnittstelle. Der Aufgabenstatus wird durch eine definierte Zustandsmaschine geprüft.
