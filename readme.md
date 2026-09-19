# Ziel
Ziel dieses Projekts ist die Entwicklung eines eigenständigen Entwickler-Harnesses in Python. Das Harness soll Markdown-Dateien als zentrale Spezifikation und Wissensgrundlage verwenden. Diese Dateien beschreiben die gewünschte Architektur, Ordnerstruktur, Konfiguration, einzubindenden Dienste und Entwicklungsabläufe.

Auf dieser Basis soll Codex in der Lage sein, ein vollständiges Entwicklungsprojekt strukturiert aufzubauen, notwendige Dateien und Verzeichnisse anzulegen, lokale Dienste wie Obsidian und SQLite einzubinden sowie definierte Entwicklungsaufgaben weitgehend selbstständig abzuarbeiten.

Konkrete Einstellungen — beispielsweise Pfade, IP-Adressen, Ports, Datenbankparameter und weitere Harness-Konfigurationen — werden über eine .env- oder YAML-Datei bereitgestellt. Dadurch sollen projektspezifische Werte von der eigentlichen Logik getrennt, reproduzierbare Entwicklungsumgebungen ermöglicht und spätere Anpassungen vereinfacht werden.

Die Markdown-Dateien bilden dabei die maßgebliche Quelle für Anforderungen, Architekturentscheidungen, Arbeitsabläufe und Projektregeln. Das Harness interpretiert diese Vorgaben, plant daraus die erforderlichen Schritte und führt sie kontrolliert innerhalb des Entwicklungsprojekts aus.