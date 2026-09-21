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

## Inhalt

+ - `permissions.py`, `command_policy.py`, `secrets.py` und `rotation.py`
