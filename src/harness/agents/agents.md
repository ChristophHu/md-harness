# Task-Agenten

Die Agentenschicht ist optional (`agents.enabled: false` als Vorgabe). Ohne sie
bleibt die deterministische Planner-/Executor-Pipeline unverändert. Bei
Aktivierung wählt der Orchestrator für einen Task ein passendes, aktiviertes
Profil: zuerst die jüngste aktive `task_assignments`-Zuweisung vom Typ
`execution`, sonst `tasks.assigned_agent`, sonst das erste passende Profil in
Konfigurationsreihenfolge. Fehlt ein Profil, geht der Task in einen prüfbaren
WAIT-Zustand.

`registry.py` validiert die Profile. Beim ersten beanspruchten Lauf wird Name,
Version und Fingerprint in `agent_task_bindings` festgehalten. Eine geänderte
Profilkonfiguration oder das Abschalten der Agentenschicht blockiert spätere
Läufe und Resumes; ein vorhandener Task fällt **nicht** still auf den
deterministischen Planner zurück. Neue Tasks können weiterhin ohne Agenten
bearbeitet werden, wenn die Schicht deaktiviert ist.
Eine bereits gebundene Aufgabe kann über `AgentStore.assign()` nicht neu
zugewiesen werden; auch eine konkurrierende externe Änderung wird vor dem
nächsten Tool-Aufruf erneut geprüft.

`runner.py` lässt das Modell nur einen strukturierten Plan erzeugen. Das Modell
erhält keine API-Tools zum direkten Aufrufen. Alle geplanten Schritte werden
mit bekannten Tools und Argumenten validiert; die tatsächlich verfügbaren
Tools sind die Schnittmenge aus Profil und Tool-Registry. Effektbehaftete
Schritte erhalten `requires_approval` und laufen erst nach einer an Plan,
Schritt, Argumente und Scope gebundenen Freigabe. Der vorhandene Executor
übernimmt Tool-Aufruf, Fencing, Journal, WAIT, Cancel und Crash-Recovery.

Die derzeitige Modellanbindung nutzt die OpenAI Responses API mit einem
strukturierten JSON-Plan. `openai_api_key` wird über den konfigurierten
Secret-Provider bezogen. Taskbeschreibung, Kriterien und ein begrenzter Teil
der Vault-Kenntnis gehen bei aktivierter Agentenschicht an den Modellanbieter;
vor dem Einschalten sind Datenschutz, Kosten, Modellzugriff und Tool-Scopes
projektbezogen zu prüfen. Ein API-Schlüssel oder Modellzugriff wird durch diese
Implementierung nicht bereitgestellt.

Ein Agent plant pro Orchestrator-Zyklus; Resultate und Validierungsfeedback
fließen beim Replan wieder ein. Frei laufende, unprotokollierte Tool-Aufrufe
oder Agent-zu-Agent-Delegation sind absichtlich nicht enthalten.
