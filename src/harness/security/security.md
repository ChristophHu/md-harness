# security

## Gitflow-Durchsetzung

`gitflow_policy.py` validiert alle vom Harness ausgelösten Branch-Erstellungen,
Commits, Pushes und Merges. Direkte Commits und Pushes auf `main` und `develop`
werden abgelehnt. Erlaubte Übergänge sind `feature/* → develop`,
`release/* → develop|main` und `hotfix/* → develop|main`.

Die versionierten Hooks unter `.githooks/` führen Qualitätsprüfungen vor Commits
und Tests mit mindestens 90 Prozent Coverage vor Pushes aus. Sie werden mit
`uv run scripts/install_git_hooks.py` in einem lokalen Checkout aktiviert.

GitHub-Branchschutz wird durch `scripts/configure_github_branch_protection.py`
für `main` und `develop` eingerichtet. Dafür ist ein `GITHUB_TOKEN` mit
Repository-Administrationsrecht erforderlich; das Secret gehört in die macOS
Keychain oder die lokale Umgebung, nicht in das Repository.

## Secret-Provider und GitHub-Authentifizierung

Secrets werden ausschließlich über `SecretProvider` aufgelöst:

- `MacOSKeychainSecretProvider` liest lokale Secrets aus der macOS-Keychain.
- `EnvironmentSecretProvider` liest Secrets aus der Umgebung und ist für CI
  vorgesehen.
- `ChainedSecretProvider` verwendet zuerst die Keychain und fällt anschließend
  auf die Umgebung zurück.

`default_secret_provider()` ist die Standardverdrahtung für lokale Harness-
Läufe. Das Mapping lautet standardmäßig `dev-harness/<secret-name>` als
Keychain-Service und der aktuelle macOS-Benutzer als Account. Es kann unter
`secrets.service_prefix` und `secrets.account` überschrieben werden. Ein
logischer Name aus `secrets.names`, zum Beispiel `github_token`, wird auf den
konfigurierten Namen `GITHUB_TOKEN` abgebildet; derselbe Zielname wird für den
Keychain-Service (`dev-harness/github-token`) und den Umgebungs-Fallback
verwendet.

Ein Keychain-Eintrag lässt sich mit
`security add-generic-password -a "$USER" -s "dev-harness/github-token" -U -w`
anlegen. Das Secret wird interaktiv abgefragt und steht nicht als
Prozessargument zur Verfügung. Das Harness verwendet dieselbe Form und reicht
den Wert über stdin. Nicht gefundene Einträge erlauben den Environment-
Fallback; verweigerter Zugriff und andere Keychain-Fehler werden als
`SecretError` gemeldet und nicht als „fehlt“ verschleiert.

Secrets dürfen niemals im `ExecutionContext`, in SQLite, Events,
Artefakten, Logs oder Git landen. Auch Fehlermeldungen dürfen Secret-Werte
nicht enthalten.

Für GitHub Actions wird die macOS-Keychain nicht verwendet. GitHub-Zugriffe
erfolgen über den von GitHub bereitgestellten `GITHUB_TOKEN` oder über ein
Repository-Secret. Ein persönlicher API-Key wird nicht in den Harness oder in
den Workflow-Code eingebaut.

## Inhalt

+ - `permissions.py`, `command_policy.py`, `secrets.py` und `rotation.py`
