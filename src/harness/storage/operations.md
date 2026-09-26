# Betrieb und Wartung

## Diagnose und Alarmierung

`harness --config config/config.yaml doctor` prüft die SQLite-Integrität und
Schema-Version und meldet aktive Tasks, alte aktive Tasks, abgelaufene Task- und
Resume-Leases, offene beziehungsweise blockierte Vault-Outbox-Einträge sowie
ungeklärte Tool-Aufrufe. Der Befehl ist read-only. `waiting`-Tasks werden
gezählt, aber nicht allein wegen ihres Alters als Fehler eingestuft: Approval,
HITL und externe Voraussetzungen können absichtlich warten.

Die Ausgabe ist JSON. Exit-Code 0 bedeutet, dass alle geprüften Zustände sauber
sind; Exit-Code 1 signalisiert mindestens eine Warnung oder einen kritischen
Befund. Ein Scheduler kann den Befehl regelmäßig ausführen und den Exit-Code
oder JSON-Ausgabestrom an das vorhandene Monitoring weitergeben. Schwellenwerte
stehen im YAML unter `operations`:

```yaml
operations:
  stale_task_minutes: 30
  outbox_pending_minutes: 15
  backup_directory: backups
  backup_keep: 7
  recovery_limit: 25
  auto_dispatch: false
  launchd_label: com.mdharness.operations
  maintenance_interval_seconds: 300
  backup_interval_seconds: 3600
  restore_drill_interval_seconds: 604800
```

`doctor` ist strikt read-only. `maintenance` darf verwaiste aktive Läufe
übernehmen; `blocked`-Entscheidungen und ungeklärte Tool-Wirkungen erfordern
weiterhin fachliche Prüfung.

## Logs und Metriken

CLI-Läufe schreiben strukturierte JSON-Ereignisse nach stderr. `task.command.completed`
enthält Task-ID, Kommando, Ergebnis und Laufzeit. `operations.doctor.completed`
enthält den Gesamtstatus und die erhobenen Zähler. Prompts, Secret-Werte und
Tool-Ausgaben werden nicht als Betriebslogs ausgegeben. SQLite-Events bleiben
der fachliche Audit-Trail; stdout enthält weiterhin das Ergebnis der CLI.

`maintenance` ist der periodische Einzelhost-Runner. Beim Application-Start
werden fällige Vault-Outbox-Einträge erneut publiziert. Danach sucht er aktive
Tasks, die älter als `stale_task_minutes` sind und keinen gültigen Claim mehr
haben, und ruft für höchstens `recovery_limit` Tasks `resume()` auf. Die
bestehenden Lease-/Fencing-Prüfungen entscheiden atomar, welcher Prozess
übernehmen darf. `waiting`-Tasks werden nicht automatisch beantwortet oder
aufgelöst. Unklare Tool-Wirkungen bleiben blockiert, bis sie durch den dafür
vorgesehenen Reconciliation-Pfad belegt geklärt wurden.

Optional startet `maintenance` danach freigegebene, ausführbare `ready`-Tasks.
Das ist mit `operations.auto_dispatch: true` bewusst explizit zu aktivieren;
standardmäßig werden neue Tasks nicht gestartet. Pro Aufruf ist die Anzahl
sowohl für Recovery als auch Dispatch durch `recovery_limit` begrenzt.
Abhängigkeiten und Freigaben werden weiterhin durch Task-Store und atomaren
Run-Claim geprüft. Schreibzugriffe bleiben zusätzlich an `execution.dry_run`
und die Tool-Sicherheitsrichtlinie gebunden.

Der Diagnosecheck zählt lang laufende Tasks mit gültigem, erneuertem Claim nicht
als verwaist. Outbox-Publikationsfehler werden auch dann gemeldet, wenn der
Eintrag noch nicht das Alterslimit erreicht hat.

## Alarmierung

`harness --config config/config.yaml alert` wertet denselben Doctor-Bericht aus,
persistiert Alarmzustände getrennt von der fachlichen SQLite-Datenbank und gibt
JSON aus. Die stabilen Check-Namen dienen als Alarm-Fingerprints. Neue Befunde
werden sofort gemeldet, Wiederholungen durch `cooldown_minutes` beziehungsweise
`repeat_minutes` begrenzt; nach `escalation_minutes` wird ein weiterhin aktiver
Befund als kritisch erneut gemeldet. Behebung erzeugt eine Entwarnung. Ungeklärte
Tool-Wirkungen und blockierte Outbox gelten als kritisch; Task-/Resume-Leases
und festhängende Tasks folgen dem Doctor-Schweregrad. Bewusst wartende
Approval-/HITL-Tasks lösen allein keinen Alarm aus.

Der Standard `operations.alerts.provider: none` sendet nichts nach außen. Der
Befehl liefert dennoch Alarmdaten und einen Fehler-Exitcode bei aktiven
Befunden, sodass ein vorhandener Monitor sie übernehmen kann. Für Slack:

```yaml
operations:
  alerts:
    provider: slack
    webhook_secret: operations_slack_webhook
    state_file: ../state/alerts.json
    cooldown_minutes: 60
    escalation_minutes: 30
    repeat_minutes: 240
```

Der Webhook wird unter dem logischen Namen `operations_slack_webhook` aus dem
macOS-Keychain oder dem zugeordneten Environment-Key
`HARNESS_SLACK_WEBHOOK` gelesen; URL und Secret werden weder in Konfiguration,
Plist noch Logs gespeichert. Für LaunchAgents ist der Keychain-Eintrag zu
verwenden, da sie nicht die interaktive Shell-Umgebung erben. Der
Keychain-Service lautet bei der Beispielkonfiguration
`dev-harness/harness-slack-webhook`, der Account ist der konfigurierte Account
oder der aktuelle macOS-Benutzer. Der LaunchAgent führt `alert` im selben Intervall
wie Maintenance aus. Die Eskalation erhöht Dringlichkeit und Wiederholungen,
leitet aber nicht automatisch an wechselnde Bereitschaftsgruppen weiter. Eine
On-Call-Rotation und deren Zustellgarantie bleiben beim angebundenen
Incident-Management.

### macOS LaunchAgent aktivieren

Die Anwendung installiert keinen Scheduler automatisch. Auf macOS kann der
Betreiber benutzerspezifische LaunchAgents ausdrücklich verwalten:

```bash
harness --config /absolut/pfad/config.yaml doctor
harness --config /absolut/pfad/config.yaml service install
harness --config /absolut/pfad/config.yaml service status
harness --config /absolut/pfad/config.yaml service uninstall
```

`install` legt Agents für Maintenance und Alarmprüfung (standardmäßig alle
300 Sekunden) sowie Backups im konfigurierten Intervall in `~/Library/LaunchAgents`
an und lädt sie in die aktuelle GUI-Login-Domain. Logs stehen im `logs/`-Verzeichnis
neben der Konfiguration. Wiederholtes Installieren ersetzt nur die verwalteten
Agents mit dem konfigurierten `launchd_label`; `uninstall` entfernt ebenfalls
ausschließlich diese Agents. Der aktuelle Benutzer muss angemeldet sein.
Bei aktiviertem SSH-Offsite-Backup kommt ein wöchentlicher
`restore-drill`-Agent hinzu. Ohne `backup_interval_seconds` bleibt die bisherige
Kalenderzeit `backup_hour`/`backup_minute` erhalten.
Vor der Installation mit `provider: ssh` sind **ein manueller Backup- und
Restore-Drill-Lauf** nötig: `service install` verweigert bei fehlenden
Offsite-/Drill-Belegen den kritischen Doctor-Zustand. Die erste Ausführung des
wöchentlichen Agents erfolgt nicht sofort beim Installieren.
Konfiguration und Python-Umgebung müssen am eingetragenen absoluten Pfad
verfügbar bleiben.
Schlägt das Laden eines Agents fehl, werden die verwalteten
Plists entfernt und beide Labels entladen; die CLI meldet den Fehler, statt eine
scheinbar erfolgreiche Teilinstallation zurückzulassen. Danach kann der
Betreiber `service status` und `service install` erneut ausführen.

Maintenance und Backup verwenden je einen nicht-blockierenden Betriebssystem-
Lock neben der Datenbank. Ein zweiter gleichartiger Lauf wird protokolliert und
übersprungen; das Betriebssystem gibt den Lock nach Prozessabbruch frei.
Maintenance- und Backup-Läufe haben getrennte Locks. Die App aktiviert dabei
weder `execution.dry_run: false` noch `operations.auto_dispatch: true`.
`service install` ist eine Hoständerung und wird daher nur auf ausdrücklichen
Aufruf ausgeführt. Es verweigert die Installation, wenn `doctor` einen
kritischen Datenbankbefund meldet. Linux-Systeme können dieselben CLI-Befehle weiterhin über
einen systemeigenen Scheduler ausführen; `service` selbst unterstützt nur
macOS/launchd.

## Backup und Wiederherstellung

Ein Backup wird mit der SQLite-Online-Backup-API erstellt und unmittelbar durch
Restore in eine temporäre Datei geprüft. Die Prüfung kontrolliert SQLite
`quick_check`, Schema-Version und zentrale Tabellen. Erst danach wird es als
`harness-<UTC-Zeitstempel>.sqlite` abgelegt beziehungsweise die Rotation
ausgeführt. Es werden ausschließlich passende `harness-*.sqlite`-Dateien im
konfigurierten Backup-Verzeichnis rotiert; andere Dateien bleiben unberührt.
Die lokalen SQLite-Dateien sind weiterhin **unverschlüsselt** und werden mit
Dateimodus 0600 angelegt. Das Verzeichnis und seine Datenträger müssen trotzdem
zugriffsgeschützt sein.

```bash
harness --config config/config.yaml maintenance
harness --config config/config.yaml backup
harness --config config/config.yaml verify-backup backups/harness-....sqlite
harness --config config/config.yaml restore-backup backups/harness-....sqlite state/recovered.sqlite
```

`restore-backup` verweigert ein bereits existierendes Ziel und prüft sowohl das
Quellbackup als auch die restaurierte Datei. Es stellt absichtlich nicht
automatisch über die konfigurierte operative Datenbank wieder her; nach Prüfung
kann der Betreiber den Zielpfad kontrolliert in Betrieb nehmen.

Für den Zielwert **RPO 2 Stunden** läuft `backup` stündlich. **RTO 4 Stunden**
ist ein zu prüfendes Betriebsziel, keine durch den Code garantierte Zeit.
`operations.backup_keep` regelt nur die lokalen Klartextkopien. Der externe
SSH-Speicher benötigt eine eigenständige, gegen Löschen geschützte
Aufbewahrungsregel (beispielsweise 30 Tage mit versionierten/immutablen
Snapshots); der Harness löscht dort keine Artefakte. SSH-Ziel und Schlüssel
müssen vor dem produktiven Start bereitgestellt werden:

```yaml
operations:
  backup_interval_seconds: 3600
  restore_drill_interval_seconds: 604800
  offsite:
    provider: ssh
    target: backup-user@backup-host.example
    directory: /srv/md-harness/backups
    key_secret: backup_encryption_key
    receipt_file: ../state/offsite-backup.json
    drill_file: ../state/restore-drill.json
    backup_max_age_minutes: 120
    drill_max_age_days: 8
    includes:
      vault: ~/md-harness-vault
      configuration: config.yaml
```

`includes` muss alle nicht aus Git rekonstruierbaren Projekt-/Arbeitsdateien
explizit benennen. Symlinks und fehlende Pfade führen zum Fehler; die Liste ist
vor Inbetriebnahme mit dem tatsächlichen Vault und den Projektverzeichnissen
abzugleichen. Die Kopie ist ein konsistenter SQLite-Snapshot, aber die
zusätzlich aufgenommenen Dateien werden während des Packens nicht eingefroren:
Schreibprozesse für diese Verzeichnisse während des Backup-Fensters anhalten
oder deren eigene Snapshot-Funktion verwenden. Die `.env` wird nicht implizit
mitgesichert; stattdessen Secret-Provider, Schlüsselmaterial, SSH-Zugang und
bekannte Host-Keys separat dokumentiert und geschützt wiederherstellbar halten.

Der Schlüssel ist ein Base64-kodierter, zufälliger 32-Byte-Wert unter dem
logischen Namen `backup_encryption_key` (Beispiel-Keychain-Service
`dev-harness/harness-backup-encryption-key`). Für LaunchAgents ist Keychain
statt einer nur interaktiv gesetzten Umgebungsvariablen zu verwenden.
Schlüsselverlust macht alle verschlüsselten Kopien unbrauchbar; Schlüsselrotation
erfordert eine dokumentierte Aufbewahrung alter Schlüssel bis zum Ablauf aller
alten Backups. Der Zielhost muss mit Public-Key-Authentisierung ohne Prompt
erreichbar sein; dessen Host-Key muss vorab in `known_hosts` geprüft und
festgelegt sein. Die SSH-Übertragung nutzt Batch-Modus und StrictHostKeyChecking.
Der Zielhost erhält nur AES-256-GCM-authentifizierten Chiffretext. Nach dem
Upload liest der Harness die Kopie vom Zielhost zurück, prüft SHA-256,
entschlüsselt und prüft alle enthaltenen Dateien sowie den SQLite-Restore.
Auch das aktuelle Inventar wird verschlüsselt als `latest.manifest` extern
abgelegt und zurückgelesen. Ein Ersatzhost kann daher ohne den verlorenen
lokalen Beleg das letzte Artefakt finden; dafür müssen Konfiguration,
SSH-Zugang und Entschlüsselungsschlüssel separat wiederbeschafft werden.
Erst danach schreibt der Harness den lokalen Erfolgsbeleg. Der `doctor`-Check meldet
fehlenden/älteren Offsite-Beleg oder Restore-Drill als kritisch; `alert` übernimmt
die Checks und deren Eskalation.

```bash
harness --config config/config.yaml backup
harness --config config/config.yaml restore-drill
harness --config config/config.yaml restore-offsite /neuer/pfad/recovery
harness --config config/config.yaml doctor
```

`restore-drill` lädt das letzte nachweislich erfolgreiche Offsite-Artefakt
erneut herunter und restauriert es ausschließlich in einem temporären
Verzeichnis. Der Beleg enthält Identität, Zeit, geprüfte Dateien,
Schema-Version und gemessene Dauer. Ein fehlgeschlagener Drill überschreibt
den letzten Erfolgsbeleg nicht; der CLI-Exitcode ist ungleich null. Für den
`restore-offsite` führt dieselben Prüfungen aus und legt anschließend die
geprüfte Datenbank sowie die `includes/`-Dateien in einem **neuen** privaten
Zielverzeichnis ab. Ein bereits existierendes Ziel wird nie überschrieben.
Für den
monatlichen **vollständigen** Restore-Test müssen Betreiber zusätzlich einen
isolierten Ersatzhost mit getrennten Zugängen verwenden und die folgenden
Runbook-Schritte samt Start-/Endzeit, Befunden und Freigabe dokumentieren.

Beispiel für cron (Pfade an die Installation anpassen):

```cron
*/5 * * * * /opt/md-harness/.venv/bin/harness --config /opt/md-harness/config/config.yaml maintenance
15 * * * * /opt/md-harness/.venv/bin/harness --config /opt/md-harness/config/config.yaml backup
30 3 * * 1 /opt/md-harness/.venv/bin/harness --config /opt/md-harness/config/config.yaml restore-drill
```

## Recovery-Runbook

1. Alarm-Fingerprint, Schweregrad, Zeitstempel und Exit-Code sichern. Niemals
   Webhook-URL, Secret, Prompt oder Tool-Ausgabe in ein Ticket kopieren.
2. `doctor` ausführen und JSON-Ausgabe samt Exit-Code sichern. `alert` erneut
   ausführen, um Zustand und Entwarnungen zu synchronisieren.
3. Bei `database_access`, `database_integrity` oder `schema_version` keine
   Recovery-/Maintenance-Läufe starten. Schreibzugriffe stoppen, eine
   dateibasierte SQLite-Sicherung erstellen und Integrität/Schema zunächst
   isoliert untersuchen.
4. Für `stale_active_tasks`, `expired_task_claims` oder `expired_resume_claims`
   Task-Events, Attempts und Claim-Zeitpunkte prüfen. `maintenance` nur dann
   verwenden, wenn Datenbank und Schema gesund sind; die vorhandenen Fencing-
   Claims entscheiden weiterhin über eine zulässige Übernahme.
5. Für `outbox_blocked`, `outbox_publish_failures` oder alte Pending-Einträge
   Vault-Erreichbarkeit, Rechte und Writer-Logs prüfen. Outbox-Zeilen nicht
   manuell löschen oder als publiziert markieren.
6. Für `unresolved_tool_invocations` den externen Zielzustand anhand des
   Tool-Audits belegen. Keine automatische Wiederholung: erst danach den
   vorgesehenen Reconciliation-Pfad nutzen oder die Entscheidung eskalieren.
7. Nach Behebung `doctor` und `alert` erneut ausführen; Entwarnung, Taskstatus,
   Outbox und Schema-Version bestätigen. Bleibt ein kritischer Befund bestehen,
   Verantwortliche eskalieren und Scheduler-Recovery ausgesetzt lassen.

### Alarmierungsübung vor Produktivbetrieb

Auf einer Testdatenbank und einem dedizierten Slack-Testkanal mindestens einen
stale-Task-/Lease-Befund, `outbox_blocked` und eine ungeklärte Tool-Wirkung
simulieren. Für jeden Fall prüfen: erster Alarm, Unterdrückung innerhalb des
Cooldowns, Eskalation nach `escalation_minutes`, Wiederholung nach
`repeat_minutes`, Entwarnung nach Bereinigung sowie Verhalten bei falschem oder
fehlendem Webhook-Secret und Netzwerkausfall. Diese Übungen und die
On-Call-Rufkette liegen beim Betreiber; die App implementiert keine
Bereitschaftsrotation.

### Wiederherstellung

1. Für SQLite- oder Schemafehler zunächst die Datenbankdatei kopieren und
   Schreibzugriffe auf die betroffene Instanz stoppen.
2. Passendes Backup mit `verify-backup` prüfen, bevor es für eine Wiederherstellung
   verwendet wird.
3. Vor dem Restore die aktuelle Datenbank separat sichern. In einen neuen,
   expliziten Pfad wiederherstellen und diesen prüfen; das CLI überschreibt keine
   operative Datenbank automatisch.
4. Nach Recovery `doctor` erneut ausführen und Datenbank, Schema-Version,
   Taskstatus sowie Outbox-Zustand bestätigen.

Für den monatlichen Offsite-Gesamttest: Alarm-/Incident-Startzeit notieren,
Originalhost unangetastet lassen, Ersatzhost bereitstellen, Schlüssel und
SSH-Zugang aus getrennt gesicherter Betreiberquelle beschaffen und den
`restore-offsite /neuer/pfad/recovery` vom Ersatzhost gegen dieselbe Remote-Kopie
ausführen. Danach die geprüfte Datenbank und `includes/`-Dateien kontrolliert
in Vault-/Konfigurationspfade des Ersatzhosts übernehmen; niemals ungeprüfte
Archivpfade über vorhandene Produktionsdateien extrahieren. Konfiguration,
Secret-Zugriff und Git-Revision herstellen, `doctor` ausführen, repräsentative
Projektentscheidungen und Outbox-Einträge prüfen und einen ungefährlichen
Test-Task durchlaufen lassen. Endzeit, Datenstand des Backups (tatsächliches
RPO), Wiederanlaufdauer (tatsächliches RTO), fehlende Dateien und Korrekturen
im Betriebsprotokoll festhalten. Bei RPO >2h oder RTO >4h ist die Übung
fehlgeschlagen und die Alarm-/Runbook-Kette nachzuarbeiten. Eine automatische
Produktions-Umschaltung oder Überschreibung existiert bewusst nicht.

Die Anwendung bietet bewusst keinen eigenen Daemon-Scheduler und keinen
E-Mail-Adapter. Auf macOS ist der LaunchAgent explizit installierbar; Slack ist
ein optionaler Zustelladapter. Andere Scheduler und Incident-Management-Systeme
können den `alert`-Befehl, seine JSON-Ausgabe und Exit-Codes verwenden.
