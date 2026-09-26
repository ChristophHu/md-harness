# migrations

Versionierte SQL-Änderungen für die SQLite-Datenbank. Jede Datei verwendet das Format `NNN_name.sql` und wird bis zur jeweiligen `PRAGMA user_version` ausgeführt.

## Inhalt

- `001_initial.sql`: initiale Schema-Version und Metadaten
- `014_step_approvals.sql`: tool- und plan-gebundene Approval-Gates
- `015_human_interactions.sql`: persistierte strukturierte HITL-Anfragen und Antworten
- `016_vault_decision_outbox.sql`: stabile Projektschlüssel und transaktionale Veröffentlichungs-Outbox
- `017_tool_invocations.sql`: durable Tool-Step-Ausführungen und manuelle Wiederanlauf-Prüfung
- `018_agent_profile_bindings.sql`: unveränderliche Bindung von Tasks an Agentenprofile

Neue Migrationen werden ausschließlich durch eine höhere Versionsnummer ergänzt. Bestehende Migrationsdateien werden nicht nachträglich verändert.

Die Migration 018 speichert Name, Version und Fingerprint des ausgewählten
Agentenprofils. Resumes mit geänderter Profilkonfiguration werden dadurch
sicher blockiert.
