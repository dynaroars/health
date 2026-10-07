#!/usr/bin/env bash
# Set up this machine to send heartbeats to the health dashboard.
#
# Usage:
#   bash setup-heartbeat.sh [slug]
#   GH_TOKEN=github_pat_xxx bash setup-heartbeat.sh [slug]
#
# - slug defaults to this machine's short hostname (prompted if interactive)
# - token is read from $GH_TOKEN, an existing env file, or a silent prompt;
#   it is stored only in ~/.config/health-heartbeat/env (mode 600)
# - safe to re-run: replaces the existing crontab entry for this script
set -euo pipefail

REPO="${REPO:-dynaroars/health}"
BRANCH_MAIN="main"
DIR="$HOME/.config/health-heartbeat"
ENV_FILE="$DIR/env"
SCRIPT="$DIR/heartbeat.py"
LOG="/tmp/health-heartbeat.log"
MARK="# health-heartbeat"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v crontab >/dev/null || { echo "crontab is required (install cron)" >&2; exit 1; }

# --- slug ---
slug="${1:-}"
if [ -z "$slug" ]; then
  default="$(hostname -s | tr '[:upper:]' '[:lower:]')"
  read -r -p "Machine slug [$default]: " slug
  slug="${slug:-$default}"
fi
[[ "$slug" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo "Invalid slug '$slug' (use a-z, 0-9, - _)" >&2; exit 1; }

# --- token ---
mkdir -p "$DIR"; chmod 700 "$DIR"
token="${GH_TOKEN:-}"
if [ -z "$token" ] && [ -f "$ENV_FILE" ]; then
  token="$(sed -n 's/^GH_TOKEN=//p' "$ENV_FILE" | head -1)"
  [ -n "$token" ] && echo "Reusing token from $ENV_FILE"
fi
if [ -z "$token" ]; then
  read -r -s -p "GitHub fine-grained token (Contents: read/write on $REPO): " token
  echo
fi
[ -n "$token" ] || { echo "No token provided" >&2; exit 1; }
umask 077
printf 'GH_TOKEN=%s\nREPO=%s\n' "$token" "$REPO" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

# --- fetch heartbeat.py ---
url="https://raw.githubusercontent.com/$REPO/$BRANCH_MAIN/scripts/heartbeat.py"
if ! curl -fsSL "$url" -o "$SCRIPT" 2>/dev/null; then
  # private repo: use the contents API with the token
  curl -fsSL -H "Authorization: Bearer $token" -H "Accept: application/vnd.github.raw" \
    "https://api.github.com/repos/$REPO/contents/scripts/heartbeat.py?ref=$BRANCH_MAIN" -o "$SCRIPT" \
    || { echo "Could not download heartbeat.py from $REPO" >&2; exit 1; }
fi

# --- test run ---
echo "Sending test heartbeat as '$slug'..."
if ( set -a; . "$ENV_FILE"; set +a; python3 "$SCRIPT" "$slug" ); then
  echo "Test heartbeat OK"
else
  echo "Test heartbeat FAILED — check the token/repo permissions. Cron not installed." >&2
  exit 1
fi

# --- cron (idempotent) ---
py="$(command -v python3)"
line="*/5 * * * * set -a; . $ENV_FILE; set +a; $py $SCRIPT $slug >> $LOG 2>&1 $MARK"
( crontab -l 2>/dev/null | grep -vF "$MARK" || true; echo "$line" ) | crontab -

echo "Installed cron job (every 5 min), log: $LOG"
echo
echo "Last step (once, on main): make sure config/monitors.yml has:"
echo "  - name: <Display Name>"
echo "    group: Machines"
echo "    description: <short description>"
echo "    type: heartbeat"
echo "    slug: $slug"
