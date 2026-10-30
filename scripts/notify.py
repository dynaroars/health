#!/usr/bin/env python3
"""Open a GitHub issue for each current problem in status.json and close it
once the problem clears. GitHub emails repo watchers about new issues, so this
is the alerting channel -- no external service needed.

Problems are: a monitor down for at least ALERT_AFTER_MIN minutes (shorter
blips are ignored), or a warning from check.monitor_warnings (disk nearly
full, TLS certificate about to expire).

Usage (needs the gh CLI and GH_TOKEN with issues: write):
    python scripts/notify.py data/status.json
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

from check import format_duration, monitor_warnings

ALERT_AFTER_MIN = 30
TITLE_PREFIX = "[health] "


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def current_problems(status: dict) -> dict[str, str]:
    """Map issue title -> issue body for every problem that should be open."""
    now = datetime.now(timezone.utc)
    problems = {}
    for m in status.get("monitors", []):
        if m["status"] == "down" and m.get("down_since"):
            since = datetime.fromisoformat(m["down_since"].replace("Z", "+00:00"))
            mins = (now - since).total_seconds() / 60
            if mins >= ALERT_AFTER_MIN:
                problems[f"{TITLE_PREFIX}{m['name']} is down"] = (
                    f"**{m['name']}** has been down for {format_duration(mins)} "
                    f"(since {m['down_since']}).\n\nLast check: {m.get('message', 'n/a')}"
                )
        for w in monitor_warnings(m):
            kind = w.split()[0]  # "disk" or "certificate": stable across changing numbers
            problems[f"{TITLE_PREFIX}{m['name']}: {kind} warning"] = f"**{m['name']}**: {w}."
    return problems


def main() -> int:
    with open(sys.argv[1]) as f:
        status = json.load(f)
    problems = current_problems(status)

    open_issues = json.loads(gh("issue", "list", "--state", "open", "--limit", "200", "--json", "number,title"))
    open_titles = {i["title"]: i["number"] for i in open_issues if i["title"].startswith(TITLE_PREFIX)}

    for title, body in problems.items():
        if title not in open_titles:
            gh("issue", "create", "--title", title, "--body", body)
            print(f"opened: {title}")

    for title, number in open_titles.items():
        if title not in problems:
            gh("issue", "close", str(number), "--comment", "Resolved automatically: the problem has cleared.")
            print(f"closed: {title}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
