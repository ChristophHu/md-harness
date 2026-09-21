"""Configure required GitHub checks for the configured Gitflow branches."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

REPOSITORY = "ChristophHu/md-harness"
BRANCHES = ("main", "develop")
PAYLOAD = {
    "required_status_checks": {"strict": True, "contexts": ["quality"]},
    "enforce_admins": True,
    "required_pull_request_reviews": {
        "dismissal_restrictions": {},
        "dismiss_stale_reviews": True,
        "require_code_owner_reviews": False,
        "required_approving_review_count": 1,
    },
    "restrictions": None,
    "required_linear_history": False,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "required_conversation_resolution": True,
}


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit(
            "GITHUB_TOKEN is required to configure GitHub branch protection"
        )
    for branch in BRANCHES:
        request = Request(
            f"https://api.github.com/repos/{REPOSITORY}/branches/{branch}/protection",
            data=json.dumps(PAYLOAD).encode(),
            method="PUT",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urlopen(request) as response:
            print(f"Protected {branch}: {response.status}")


if __name__ == "__main__":
    main()
