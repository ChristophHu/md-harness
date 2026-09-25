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
  backup_hour: 2
  backup_minute: 15
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
Betreiber zwei benutzerspezifische LaunchAgents ausdrücklich verwalten:

```bash
harness --config /absolut/pfad/config.yaml doctor
harness --config /absolut/pfad/config.yaml service install
harness --config /absolut/pfad/config.yaml service status
harness --config /absolut/pfad/config.yaml service uninstall
```

`install` legt Agents für Maintenance und Alarmprüfung (standardmäßig alle
300 Sekunden) sowie tägliche Backups um 02:15 Uhr in `~/Library/LaunchAgents`
an und lädt sie in die aktuelle GUI-Login-Domain. Logs stehen im `logs/`-Verzeichnis
neben der Konfiguration. Wiederholtes Installieren ersetzt nur die beiden
Agents mit dem konfigurierten `launchd_label`; `uninstall` entfernt ebenfalls
ausschließlich diese Agents. Der aktuelle Benutzer muss angemeldet sein.
Konfiguration und Python-Umgebung müssen am eingetragenen absoluten Pfad
verfügbar bleiben.
Schlägt das Laden eines der beiden Agents fehl, werden die beiden verwalteten
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

Für regelmäßige Backups wird der Befehl über cron, launchd oder einen anderen
Betriebssystem-Scheduler gestartet. Die Aufbewahrung ist konfigurierbar über
`operations.backup_keep`. Das Backup-Verzeichnis sollte auf einem separaten,
zugriffsgeschützten und ausreichend verfügbaren Datenträger liegen. Ein
regelmäßiger Restore-Test in einem isolierten Ziel ist zusätzlich zum
automatischen temporären Selbsttest sinnvoll; Aufbewahrung und externe
Kopie/Offsite-Strategie sind Betreiberverantwortung.

Beispiel für cron (Pfade an die Installation anpassen):

```cron
*/5 * * * * /opt/md-harness/.venv/bin/harness --config /opt/md-harness/config/config.yaml maintenance
15 2 * * * /opt/md-harness/.venv/bin/harness --config /opt/md-harness/config/config.yaml backup
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

Die Anwendung bietet bewusst keinen integrierten Scheduler und keinen
E-Mail-Adapter. Auf macOS ist der LaunchAgent explizit installierbar; Slack ist
ein optionaler Zustelladapter. Andere Scheduler und Incident-Management-Systeme
können den `alert`-Befehl, seine JSON-Ausgabe und Exit-Codes verwenden.
