#!/usr/bin/env python3
"""Run HTTP/TCP checks defined in a YAML config, update rolling history,
and write out status.json + history.json for the static dashboard.

Usage:
    python scripts/check.py --config config/monitors.yml --data-dir data
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlsplit

import yaml

DEFAULT_TIMEOUT = 10
DEFAULT_HEARTBEAT_MAX_AGE = 900  # seconds; 3x a 5-minute push interval
RETENTION_DAYS_DEFAULT = 30
BAR_SAMPLES_DEFAULT = 50
INCIDENT_LIMIT_DEFAULT = 20
UPTIME_WINDOWS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: str) -> list[dict]:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}
    monitors = raw.get("monitors") or []
    seen = set()
    for m in monitors:
        if "name" not in m or "type" not in m:
            raise ValueError(f"monitor entry missing 'name' or 'type': {m}")
        if m["name"] in seen:
            raise ValueError(f"duplicate monitor name: {m['name']!r}")
        seen.add(m["name"])
        if m["type"] not in ("http", "tcp", "heartbeat"):
            raise ValueError(f"unsupported monitor type: {m['type']!r}")
        if m["type"] == "http" and "url" not in m:
            raise ValueError(f"http monitor {m['name']!r} missing 'url'")
        if m["type"] == "tcp" and ("host" not in m or "port" not in m):
            raise ValueError(f"tcp monitor {m['name']!r} missing 'host'/'port'")
        if m["type"] == "heartbeat" and "slug" not in m:
            raise ValueError(f"heartbeat monitor {m['name']!r} missing 'slug'")
    return monitors


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_http(monitor: dict) -> dict:
    url = monitor["url"]
    timeout = monitor.get("timeout", DEFAULT_TIMEOUT)
    parts = urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    start = time.monotonic()
    conn = None
    try:
        conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
        conn.request("GET", path, headers={"User-Agent": "roars-status/1.0", "Host": parts.netloc})
        resp = conn.getresponse()
        latency_ms = round((time.monotonic() - start) * 1000)
        code = resp.status
        healthy = 200 <= code < 400
        return {
            "status": "up" if healthy else "down",
            "latency_ms": latency_ms,
            "message": f"HTTP {code}",
        }
    except Exception as e:  # noqa: BLE001 - any failure means the check is down
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "status": "down",
            "latency_ms": None,
            "message": f"{type(e).__name__}: {e}",
        }
    finally:
        if conn is not None:
            conn.close()


def check_tcp(monitor: dict) -> dict:
    host = monitor["host"]
    port = monitor["port"]
    timeout = monitor.get("timeout", DEFAULT_TIMEOUT)

    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        latency_ms = round((time.monotonic() - start) * 1000)
        return {"status": "up", "latency_ms": latency_ms, "message": f"connected to {host}:{port}"}
    except Exception as e:  # noqa: BLE001
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "status": "down",
            "latency_ms": None,
            "message": f"{type(e).__name__}: {e}",
        }


def check_heartbeat(monitor: dict, data_dir: str) -> dict:
    """A "pull-less" check for machines GitHub Actions can't reach directly
    (e.g. behind a VPN). The machine itself pushes a last-seen timestamp to
    data/heartbeats/<slug>.json via the GitHub Contents API (see
    scripts/heartbeat.py); we just judge whether that timestamp is fresh.
    """
    max_age = monitor.get("max_age", DEFAULT_HEARTBEAT_MAX_AGE)
    path = os.path.join(data_dir, "heartbeats", f"{monitor['slug']}.json")
    if not os.path.exists(path):
        return {"status": "down", "latency_ms": None, "message": "no heartbeat received yet"}
    try:
        with open(path, "r") as f:
            last_seen = datetime.fromisoformat(json.load(f)["last_seen"].replace("Z", "+00:00"))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        return {"status": "down", "latency_ms": None, "message": f"invalid heartbeat file: {e}"}
    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
    healthy = 0 <= age <= max_age
    return {
        "status": "up" if healthy else "down",
        "latency_ms": None,
        "message": f"last heartbeat {round(age)}s ago" if healthy else f"stale heartbeat ({round(age)}s ago, max {max_age}s)",
    }


def run_check(monitor: dict, data_dir: str) -> dict:
    if monitor["type"] == "http":
        result = check_http(monitor)
    elif monitor["type"] == "tcp":
        result = check_tcp(monitor)
    else:
        result = check_heartbeat(monitor, data_dir)
    result["name"] = monitor["name"]
    result["type"] = monitor["type"]
    return result


def run_checks(monitors: list[dict], workers: int, data_dir: str) -> list[dict]:
    if not monitors:
        return []
    results = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(monitors))) as pool:
        futures = {pool.submit(run_check, m, data_dir): m["name"] for m in monitors}
        for future in as_completed(futures):
            name = futures[future]
            results[name] = future.result()
    # preserve config order for a stable dashboard layout
    return [results[m["name"]] for m in monitors]


# --------------------------------------------------------------------------
# History (rolling retention)
# --------------------------------------------------------------------------

def load_history(path: str) -> dict:
    if not os.path.exists(path):
        return {"monitors": {}}
    with open(path, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {"monitors": {}}
    data.setdefault("monitors", {})
    return data


def update_history(history: dict, results: list[dict], now_ts: int, retention_days: int) -> None:
    cutoff = now_ts - retention_days * 86400
    valid_names = {r["name"] for r in results}

    # drop history for monitors that no longer exist in the config
    for name in list(history["monitors"].keys()):
        if name not in valid_names:
            del history["monitors"][name]

    for r in results:
        samples = history["monitors"].setdefault(r["name"], [])
        samples.append([now_ts, 1 if r["status"] == "up" else 0, r["latency_ms"]])
        # prune old samples (samples are appended in increasing ts order, so
        # this is a cheap linear scan from the front)
        i = 0
        while i < len(samples) and samples[i][0] < cutoff:
            i += 1
        if i:
            del samples[:i]


def uptime_pct(samples: list[list], now_ts: int, window_s: int) -> float | None:
    window = [s for s in samples if s[0] >= now_ts - window_s]
    if not window:
        return None
    up = sum(1 for s in window if s[1] == 1)
    return round(up / len(window) * 100, 2)


def compute_incidents(name: str, samples: list[list], limit: int) -> list[dict]:
    incidents = []
    current = None
    for ts, status, _latency in samples:
        if status == 0 and current is None:
            current = {"monitor": name, "start": ts, "end": None}
        elif status == 1 and current is not None:
            current["end"] = ts
            incidents.append(current)
            current = None
    if current is not None:
        incidents.append(current)
    return incidents[-limit:]


# --------------------------------------------------------------------------
# Status output
# --------------------------------------------------------------------------

def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_status(results: list[dict], history: dict, now_ts: int, bar_samples: int, incident_limit: int) -> dict:
    monitors_out = []
    all_incidents = []

    for r in results:
        samples = history["monitors"].get(r["name"], [])
        bar = [s[1] for s in samples[-bar_samples:]]
        monitors_out.append({
            "name": r["name"],
            "type": r["type"],
            "status": r["status"],
            "latency_ms": r["latency_ms"],
            "message": r["message"],
            "checked_at": iso(now_ts),
            "uptime_24h": uptime_pct(samples, now_ts, UPTIME_WINDOWS["24h"]),
            "uptime_7d": uptime_pct(samples, now_ts, UPTIME_WINDOWS["7d"]),
            "uptime_30d": uptime_pct(samples, now_ts, UPTIME_WINDOWS["30d"]),
            "history": bar,
        })
        for inc in compute_incidents(r["name"], samples, incident_limit):
            all_incidents.append({
                "monitor": inc["monitor"],
                "start": iso(inc["start"]),
                "end": iso(inc["end"]) if inc["end"] else None,
                "duration_min": round((inc["end"] - inc["start"]) / 60, 1) if inc["end"] else None,
            })

    all_incidents.sort(key=lambda i: i["start"], reverse=True)

    if all(m["status"] == "up" for m in monitors_out):
        overall = "operational"
    elif all(m["status"] == "down" for m in monitors_out):
        overall = "outage"
    else:
        overall = "degraded"

    return {
        "updated": iso(now_ts),
        "overall": overall,
        "monitors": monitors_out,
        "incidents": all_incidents[:incident_limit],
    }


def atomic_write_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2 if os.path.basename(path) == "status.json" else None, separators=(",", ":"))
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# HTML rendering (Pure HTML + org.css, zero JavaScript)
# --------------------------------------------------------------------------

def render_html(status: dict) -> str:
    monitors = status.get("monitors", [])
    incidents = status.get("incidents", [])
    overall = status.get("overall", "operational")

    if overall == "operational":
        overall_msg = "<strong>All systems operational</strong>"
    elif overall == "outage":
        overall_msg = "<strong>Major outage</strong> &mdash; all monitored services are down"
    else:
        overall_msg = "<strong>Degraded performance / partial outage</strong>"

    rows = []
    for m in monitors:
        status_str = m["status"]
        if status_str == "up":
            status_html = "<span>Operational</span>"
        else:
            status_html = "<strong>Offline</strong>"

        latency = f"{m['latency_ms']} ms" if m.get("latency_ms") is not None else "--"
        u24 = f"{m['uptime_24h']}%" if m.get("uptime_24h") is not None else "n/a"
        u7d = f"{m['uptime_7d']}%" if m.get("uptime_7d") is not None else "n/a"
        u30d = f"{m['uptime_30d']}%" if m.get("uptime_30d") is not None else "n/a"
        message = m.get("message") or ""

        rows.append(
            f"      <tr>\n"
            f"        <td><strong>{m['name']}</strong></td>\n"
            f"        <td><code>{m['type']}</code></td>\n"
            f"        <td>{status_html}</td>\n"
            f"        <td>{latency}</td>\n"
            f"        <td>{u24}</td>\n"
            f"        <td>{u7d}</td>\n"
            f"        <td>{u30d}</td>\n"
            f"        <td><small>{message}</small></td>\n"
            f"      </tr>"
        )

    table_rows = "\n".join(rows) if rows else "      <tr><td colspan='8'>No monitors configured.</td></tr>"

    incidents_html = ""
    if incidents:
        inc_items = []
        for inc in incidents:
            ongoing = inc.get("end") is None
            if ongoing:
                time_str = f"since {inc['start']} (ongoing)"
            else:
                time_str = f"{inc['start']} &ndash; {inc['end']} ({inc['duration_min']} min)"
            inc_items.append(f"    <li><strong>{inc['monitor']}</strong> &mdash; {time_str}</li>")
        incidents_list = "\n".join(inc_items)
        incidents_html = f"""
  <h2>Recent Incidents</h2>
  <ul>
{incidents_list}
  </ul>
"""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>ROARS Status</title>
  <link rel="stylesheet" type="text/css" href="files/org.css">
</head>
<body>
  <h1>ROARS Status</h1>

  <blockquote>
    {overall_msg} &mdash; checks run every 5 minutes via GitHub Actions.
  </blockquote>

  <h2>Services &amp; Machines</h2>
  <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%;">
    <thead>
      <tr style="text-align: left;">
        <th>Monitor</th>
        <th>Type</th>
        <th>Status</th>
        <th>Latency</th>
        <th>24h</th>
        <th>7d</th>
        <th>30d</th>
        <th>Details</th>
      </tr>
    </thead>
    <tbody>
{table_rows}
    </tbody>
  </table>
{incidents_html}
  <p><small>Automatically updated every 5 minutes &middot; Pure HTML &amp; CSS</small></p>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/monitors.yml")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--html-out", default="index.html", help="Path to write rendered static index.html")
    parser.add_argument("--retention-days", type=int, default=RETENTION_DAYS_DEFAULT)
    parser.add_argument("--bar-samples", type=int, default=BAR_SAMPLES_DEFAULT)
    parser.add_argument("--incident-limit", type=int, default=INCIDENT_LIMIT_DEFAULT)
    parser.add_argument("--workers", type=int, default=20)
    args = parser.parse_args()

    monitors = load_config(args.config)
    os.makedirs(args.data_dir, exist_ok=True)

    results = run_checks(monitors, args.workers, args.data_dir)
    now_ts = int(time.time())

    history_path = os.path.join(args.data_dir, "history.json")
    history = load_history(history_path)
    update_history(history, results, now_ts, args.retention_days)
    atomic_write_json(history_path, history)

    status = build_status(results, history, now_ts, args.bar_samples, args.incident_limit)
    atomic_write_json(os.path.join(args.data_dir, "status.json"), status)

    if args.html_out:
        html_dir = os.path.dirname(args.html_out)
        if html_dir:
            os.makedirs(html_dir, exist_ok=True)
        html_content = render_html(status)
        tmp_html = f"{args.html_out}.tmp"
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(html_content)
        os.replace(tmp_html, args.html_out)

    up = sum(1 for r in results if r["status"] == "up")
    print(f"checked {len(results)} monitor(s): {up} up, {len(results) - up} down")
    for r in results:
        lat_str = f"{r['latency_ms']:>4}ms" if r["latency_ms"] is not None else "      "
        print(f"  [{r['status']:>4}] {r['name']:<30} {lat_str}  {r['message']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
