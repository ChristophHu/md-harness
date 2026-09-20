# migrations

Versionierte SQL-Änderungen für die SQLite-Datenbank. Jede Datei verwendet das Format `NNN_name.sql` und wird bis zur jeweiligen `PRAGMA user_version` ausgeführt.

## Inhalt

- `001_initial.sql`: initiale Schema-Version und Metadaten

Neue Migrationen werden ausschließlich durch eine höhere Versionsnummer ergänzt. Bestehende Migrationsdateien werden nicht nachträglich verändert.
