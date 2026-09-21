"""Install the repository-managed Git hooks for the current checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    hooks = root / ".githooks"
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)],
        cwd=root,
        check=True,
    )
    print(f"Installed Git hooks from {hooks}")


if __name__ == "__main__":
    main()
