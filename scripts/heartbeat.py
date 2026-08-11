#!/usr/bin/env python3
"""Push a liveness heartbeat for this machine to the repo's `data` branch,
via the GitHub Contents API, for machines the GitHub Actions runner can't
reach directly (e.g. behind a VPN).

Run this from cron on the machine being monitored -- roughly as often as
the monitor's `max_age` in config/monitors.yml expects (every 5 minutes is
a sane default, matching the check interval).

Requires a fine-grained GitHub Personal Access Token, scoped to ONLY this
repository with "Contents: Read and write" permission, passed via the
GH_TOKEN environment variable. See README.md for how to create one and why
`main` should be branch-protected before you hand this token out.

Example crontab entry (every 5 minutes):
    */5 * * * * GH_TOKEN=github_pat_xxx REPO=dynaroars/stats /usr/bin/python3 /opt/roars/heartbeat.py taco
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BRANCH = "data"


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: heartbeat.py <slug>")
    slug = sys.argv[1]

    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("REPO")
    if not token or not repo:
        sys.exit("GH_TOKEN and REPO environment variables are required")

    path = f"data/heartbeats/{slug}.json"
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "roars-heartbeat/1.0",
    }

    sha = None
    try:
        req = urllib.request.Request(f"{api_url}?ref={BRANCH}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            sha = json.load(resp)["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    body = json.dumps({
        "last_seen": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }).encode()
    payload = {
        "message": f"heartbeat: {slug}",
        "content": base64.b64encode(body).decode(),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha

    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
    print(f"heartbeat pushed for {slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
