# ROARS Status

A minimal uptime/status dashboard that runs entirely on GitHub — no server,
no database, no container. GitHub Actions performs HTTP/TCP checks every 5
minutes and publishes a static dashboard to GitHub Pages.

```
GitHub Actions (every 5 min)
  -> scripts/check.py            HTTP + TCP checks, run concurrently
  -> data branch (history.json)  rolling 30-day sample history
  -> data branch (status.json)   latest snapshot + computed uptime %
  -> GitHub Pages (via Actions)  site/ + data/*.json, deployed as an artifact
```

## Why a separate `data` branch?

The dashboard needs history to compute 24h/7d/30d uptime, but a monitor that
runs every 5 minutes must **not** create ~288 commits/day on `main` — that
would drown out real commit history and bloat the repo you actually work in.

The chosen design: history lives as two small JSON files (`status.json`,
`history.json`) on a dedicated, orphaned **`data` branch**, updated by a bot
commit each run. `main` only ever contains code, so it stays clean. The
`data` branch does accumulate one commit every 5 minutes, but that's fine —
it's not a branch you browse or diff by hand, and old commits there cost
nothing to ignore. GitHub Pages is deployed straight from a build
artifact (`actions/upload-pages-artifact` + `actions/deploy-pages`), so
there's no `gh-pages` branch either.

**Tradeoffs considered and rejected:**
- *Actions cache/artifacts as the store*: caches are evictable (7-day
  default retention, LRU-evicted under storage pressure) and not meant for
  durable data — a cache miss would silently reset all history.
- *Committing history to `main`*: simplest to explain, but produces
  hundreds of noisy commits/day exactly as the prompt says to avoid.
- *External database (Supabase/Postgres/etc.)*: durable and query-friendly,
  but it's a persistent external dependency — the opposite of "GitHub-only".

Rolling retention (default 30 days) is enforced on every run in
`scripts/check.py`, so `history.json` never grows without bound, and any
monitor removed from `config/monitors.yml` has its history dropped on the
next run.

## Repository layout

```
.github/workflows/monitor.yml   the one workflow: check -> commit -> deploy
config/monitors.yml             monitors to check (edit this to add/remove machines)
scripts/check.py                the checker (stdlib + PyYAML only)
scripts/requirements.txt        just PyYAML
site/index.html                 dashboard markup
site/style.css                  dark, old-school, responsive styling
site/app.js                     fetches data/status.json and renders it
README.md                       this file
```

`data/` does not live on `main` — it's generated at runtime (see above), and
is gitignored locally so a local test run doesn't accidentally get committed
to the wrong branch.

## Setup

1. Push this repo to GitHub (if you haven't already).
2. In **Settings → Pages**, set **Source** to **GitHub Actions** (not "Deploy
   from a branch"). This only needs to be done once.
3. In **Settings → Actions → General → Workflow permissions**, make sure
   "Read and write permissions" is allowed for `GITHUB_TOKEN` (needed so the
   workflow can push to the `data` branch — the workflow also declares
   `permissions: contents: write` explicitly at the job level, but the repo
   default must not be set to "Read only" or that override still gets
   blocked on some org policies).
4. Edit `config/monitors.yml` with your real services (see below).
5. Commit and push. Either wait for the next 5-minute tick, or trigger the
   workflow manually: **Actions → Monitor → Run workflow**.
6. The first successful run creates the `data` branch automatically (it
   doesn't need to exist beforehand) and deploys the site. Your dashboard
   will be live at `https://<user>.github.io/<repo>/`.

## Adding / removing monitors

Edit `config/monitors.yml`. Each entry is either `http` or `tcp`:

```yaml
monitors:
  - name: ROARS Website       # must be unique — used as the history key
    type: http
    url: https://roars.dev
    timeout: 10                # seconds, optional (default 10)

  - name: Example SSH Server
    type: tcp
    host: example.roars.dev
    port: 22
    timeout: 5
```

- HTTP checks issue a `GET` and treat any `2xx`/`3xx` response as healthy
  (redirects are **not** followed — a `3xx` itself counts as "the server
  responded", which is what you want for a reachability check).
- TCP checks just open and close a TCP connection to `host:port`.
- Renaming a monitor is the same as deleting the old one and adding a new
  one — its history starts fresh under the new name.
- There's no hard limit on monitor count; checks run concurrently
  (`ThreadPoolExecutor`), so 20+ monitors finish in roughly one timeout
  period, not N timeout periods.

## How the scheduled checks work

`.github/workflows/monitor.yml` runs on a `*/5 * * * *` cron and on manual
`workflow_dispatch`. Each run:

1. Checks out `main` (code) and the `data` branch (history), as a git
   worktree — creating `data` as an orphan branch on the very first run.
2. Runs `scripts/check.py`, which checks every monitor concurrently, appends
   one sample per monitor to `history.json`, prunes samples older than the
   retention window, and writes the latest `status.json` snapshot.
3. Commits the updated `data/` files to the `data` branch (skipped if
   nothing changed, which won't normally happen since `checked_at` always
   changes, but the check exists for safety).
4. Assembles `site/*` + the freshly generated `data/*.json` into `dist/` and
   deploys it to GitHub Pages via `actions/upload-pages-artifact` +
   `actions/deploy-pages` — the current recommended approach, which deploys
   from a build artifact instead of pushing rendered HTML into a branch.

A `concurrency: group: status-monitor` block serializes runs so two
overlapping executions (e.g. a manual run firing right as the scheduled one
starts) can't race on the `data` branch push or on the Pages deployment —
the second run queues instead of running in parallel.

## Custom domain

To serve this at `uptime.roars.dev` or `status.roars.dev`:

1. **Settings → Pages → Custom domain**, enter the subdomain. GitHub commits
   a `CNAME` file into the Pages deployment automatically once this is set —
   you don't need to add one to `site/` yourself.
2. At your DNS provider, add a `CNAME` record for the subdomain pointing to
   `<user>.github.io`.
3. Wait for DNS to propagate, then enable **Enforce HTTPS** in the same
   Pages settings page once GitHub shows the certificate as issued.

## Local testing

```bash
python3 -m venv .venv && source .venv/bin/activate.fish   # or activate for bash
pip install -r scripts/requirements.txt

# run one check cycle against a local data/ directory
python scripts/check.py --config config/monitors.yml --data-dir data

# assemble and serve the dashboard exactly like the deployed site
mkdir -p dist/data
cp site/*.html site/*.css site/*.js dist/
cp data/*.json dist/data/
cd dist && python3 -m http.server 8000
# open http://localhost:8000
```

Run the check command a few times (or on a loop) to build up enough history
samples to see the uptime bar and percentages populate.

## Security

- No credentials are embedded in the frontend — `site/app.js` only ever
  fetches the already-public `data/status.json`.
- Checks are unauthenticated HTTP/TCP only. If you later need authenticated
  checks (an API key in a header, etc.), pass them into `check.py` via
  `${{ secrets.SOME_TOKEN }}` in the workflow's `env:`, read them with
  `os.environ` in Python, and never write the raw secret value into
  `status.json` or `history.json` (both files are public, served straight
  to the browser).

## Limitations

- **Not real-time.** GitHub Actions scheduled workflows have a practical
  minimum interval of about 5 minutes, and the actual trigger time can drift
  by a few minutes under GitHub's load — cron schedules are "best effort",
  not guaranteed to the minute. This is not designed for second-by-second
  monitoring or paging-grade alerting.
- No notifications (email/Slack/etc.) are built in — this is a status page,
  not an alerting system. It would be a reasonable extension to add a step
  to the workflow that notifies on a status transition.
- History resolution is one sample per check interval (5 min); there's no
  sub-interval granularity.
- IPv4/IPv6 dual-stack, DNS-round-robin, and geographically distributed
  checks aren't supported — checks run from wherever the GitHub Actions
  runner happens to be.
