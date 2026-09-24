# migrations

Versionierte SQL-Änderungen für die SQLite-Datenbank. Jede Datei verwendet das Format `NNN_name.sql` und wird bis zur jeweiligen `PRAGMA user_version` ausgeführt.

## Inhalt

- `001_initial.sql`: initiale Schema-Version und Metadaten
- `014_step_approvals.sql`: tool- und plan-gebundene Approval-Gates
- `015_human_interactions.sql`: persistierte strukturierte HITL-Anfragen und Antworten
- `016_vault_decision_outbox.sql`: stabile Projektschlüssel und transaktionale Veröffentlichungs-Outbox

Neue Migrationen werden ausschließlich durch eine höhere Versionsnummer ergänzt. Bestehende Migrationsdateien werden nicht nachträglich verändert.
