#!/usr/bin/env python3
"""Run HTTP/TCP checks defined in a YAML config, update rolling history,
compute geeky telemetry & SRE metrics, and write out a pure static HTML dashboard.

Usage:
    python scripts/check.py --config config/monitors.yml --data-dir data
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import socket
import ssl
import statistics
import subprocess
import sys
import time
import urllib.request
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
SPARK_CHARS = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


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
# Checks & TLS Inspection
# --------------------------------------------------------------------------

def inspect_tls(hostname: str, port: int = 443, timeout: int = 5) -> dict | None:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                cipher = ssock.cipher()
                version = ssock.version()
                if not cert or "notAfter" not in cert:
                    return None
                exp_date = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                days_left = (exp_date - datetime.now(timezone.utc)).days
                issuer_dict = dict(x[0] for x in cert.get("issuer", []))
                issuer = issuer_dict.get("organizationName") or issuer_dict.get("commonName") or "Unknown"
                return {
                    "version": version,
                    "cipher": cipher[0] if cipher else "Unknown",
                    "issuer": issuer,
                    "days_left": days_left,
                    "expiry": exp_date.strftime("%Y-%m-%d"),
                }
    except Exception:
        return None


def check_http(monitor: dict) -> dict:
    url = monitor["url"]
    timeout = monitor.get("timeout", DEFAULT_TIMEOUT)
    parts = urlsplit(url)
    is_https = parts.scheme == "https"
    conn_cls = http.client.HTTPSConnection if is_https else http.client.HTTPConnection
    port = parts.port or (443 if is_https else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    start = time.monotonic()
    conn = None
    server_hdr = None
    tls_info = None

    try:
        conn = conn_cls(parts.hostname, port, timeout=timeout)
        conn.request("GET", path, headers={"User-Agent": "roars-status/1.0", "Host": parts.netloc})
        resp = conn.getresponse()
        latency_ms = round((time.monotonic() - start) * 1000)
        code = resp.status
        server_hdr = resp.getheader("Server")
        healthy = 200 <= code < 400

        if is_https and parts.hostname:
            tls_info = inspect_tls(parts.hostname, port, timeout=min(timeout, 5))

        return {
            "status": "up" if healthy else "down",
            "latency_ms": latency_ms,
            "message": f"HTTP {code}",
            "server": server_hdr,
            "tls": tls_info,
        }
    except Exception as e:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "status": "down",
            "latency_ms": None,
            "message": f"{type(e).__name__}: {e}",
            "server": None,
            "tls": None,
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
        return {
            "status": "up",
            "latency_ms": latency_ms,
            "message": f"connected to {host}:{port}",
            "server": None,
            "tls": None,
        }
    except Exception as e:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "status": "down",
            "latency_ms": None,
            "message": f"{type(e).__name__}: {e}",
            "server": None,
            "tls": None,
        }


def get_auth_token() -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def check_heartbeat(monitor: dict, data_dir: str) -> dict:
    max_age = monitor.get("max_age", DEFAULT_HEARTBEAT_MAX_AGE)
    slug = monitor["slug"]
    path = os.path.join(data_dir, "heartbeats", f"{slug}.json")
    raw = None

    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                raw = json.load(f)
        except Exception:
            pass
    
    if raw is None:
        # Fallback to fetching directly from GitHub API (e.g. for local testing)
        try:
            repo = os.environ.get("REPO", "dynaroars/health")
            token = get_auth_token()
            url = f"https://api.github.com/repos/{repo}/contents/data/heartbeats/{slug}.json?ref=data"
            headers = {"User-Agent": "roars-check/1.0", "Accept": "application/vnd.github+json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                import base64
                body = base64.b64decode(json.load(resp)["content"]).decode()
                raw = json.loads(body)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(body)
        except Exception:
            pass

    if not raw or "last_seen" not in raw:
        return {
            "status": "down",
            "latency_ms": None,
            "message": "no heartbeat received yet",
            "server": None,
            "tls": None,
            "host_telemetry": None,
            "heartbeat_age_s": None,
        }

    try:
        last_seen = datetime.fromisoformat(raw["last_seen"].replace("Z", "+00:00"))
        host_telemetry = raw.get("telemetry")
    except Exception as e:
        return {
            "status": "down",
            "latency_ms": None,
            "message": f"invalid heartbeat file: {e}",
            "server": None,
            "tls": None,
            "host_telemetry": None,
            "heartbeat_age_s": None,
        }

    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
    healthy = 0 <= age <= max_age
    return {
        "status": "up" if healthy else "down",
        "latency_ms": None,
        "message": f"last heartbeat {round(age)}s ago" if healthy else f"stale heartbeat ({round(age)}s ago, max {max_age}s)",
        "server": None,
        "tls": None,
        "host_telemetry": host_telemetry,
        "heartbeat_age_s": round(age),
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
    result["url"] = monitor.get("url")
    result["host"] = monitor.get("host")
    result["port"] = monitor.get("port")
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
    return [results[m["name"]] for m in monitors]


# --------------------------------------------------------------------------
# History & Statistics (Rolling Retention)
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

    for name in list(history["monitors"].keys()):
        if name not in valid_names:
            del history["monitors"][name]

    for r in results:
        samples = history["monitors"].setdefault(r["name"], [])
        samples.append([now_ts, 1 if r["status"] == "up" else 0, r["latency_ms"]])
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
# Geeky Telemetry, Sparklines & SRE Math
# --------------------------------------------------------------------------

def make_sparkline(values: list[int | float]) -> str:
    if not values:
        return "────────"
    valid = [v for v in values if v is not None]
    if not valid:
        return "────────"
    min_v, max_v = min(valid), max(valid)
    if min_v == max_v:
        return "▅" * len(valid)
    res = []
    for v in valid:
        idx = int((v - min_v) / (max_v - min_v) * (len(SPARK_CHARS) - 1))
        res.append(SPARK_CHARS[min(idx, len(SPARK_CHARS) - 1)])
    return "".join(res)


def make_histogram(latencies: list[int | float]) -> str:
    if not latencies:
        return "  No latency data available."
    buckets = [
        ("< 100ms", lambda x: x < 100),
        ("100-200ms", lambda x: 100 <= x < 200),
        ("200-500ms", lambda x: 200 <= x < 500),
        (">= 500ms", lambda x: x >= 500),
    ]
    total = len(latencies)
    lines = []
    max_bar_width = 25
    for label, fn in buckets:
        cnt = sum(1 for x in latencies if fn(x))
        pct = (cnt / total) * 100 if total else 0
        bar_len = int((pct / 100) * max_bar_width)
        bar = "█" * bar_len
        lines.append(f"  {label:<10} [{bar:<25}] {cnt:>4} ({pct:>5.1f}%)")
    return "\n".join(lines)


def calc_telemetry(samples: list[list]) -> dict:
    # samples: [ts, status (1/0), latency_ms]
    latencies = [s[2] for s in samples if s[1] == 1 and s[2] is not None]
    
    # Streak count (consecutive 1s from the latest backwards)
    streak = 0
    for s in reversed(samples):
        if s[1] == 1:
            streak += 1
        else:
            break
    streak_hours = round(streak * 5 / 60, 1)

    # Mathematical Nines of availability
    up_count = sum(1 for s in samples if s[1] == 1)
    total_count = len(samples)
    uptime_ratio = up_count / total_count if total_count else 1.0
    if uptime_ratio >= 1.0:
        nines = "5+ nines (100%)"
    elif uptime_ratio <= 0.0:
        nines = "0 nines (0%)"
    else:
        unavail = 1.0 - uptime_ratio
        val = -math.log10(unavail)
        nines = f"{val:.2f} nines ({uptime_ratio*100:.2f}%)"

    if not latencies:
        return {
            "p50": None, "p90": None, "p95": None, "p99": None, "min": None, "max": None, "stddev": None,
            "streak": streak, "streak_hours": streak_hours, "nines": nines,
            "sparkline_24h": "────────", "histogram": "  No latency data available.", "sample_count": total_count
        }

    sorted_lat = sorted(latencies)
    def pctile(p: float) -> int:
        k = (len(sorted_lat) - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_lat[int(k)]
        return round(sorted_lat[int(f)] * (c - k) + sorted_lat[int(c)] * (k - f))

    p50 = pctile(50)
    p90 = pctile(90)
    p95 = pctile(95)
    p99 = pctile(99)
    min_lat = sorted_lat[0]
    max_lat = sorted_lat[-1]
    stddev = round(statistics.stdev(latencies), 1) if len(latencies) > 1 else 0.0
    sparkline_24h = make_sparkline(latencies[-24:])
    histogram = make_histogram(latencies)

    return {
        "p50": p50, "p90": p90, "p95": p95, "p99": p99, "min": min_lat, "max": max_lat, "stddev": stddev,
        "streak": streak, "streak_hours": streak_hours, "nines": nines,
        "sparkline_24h": sparkline_24h, "histogram": histogram, "sample_count": total_count
    }


# --------------------------------------------------------------------------
# Status Output
# --------------------------------------------------------------------------

def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_status(results: list[dict], history: dict, now_ts: int, bar_samples: int, incident_limit: int) -> dict:
    monitors_out = []
    all_incidents = []

    for r in results:
        samples = history["monitors"].get(r["name"], [])
        bar = [s[1] for s in samples[-bar_samples:]]
        telemetry = calc_telemetry(samples)

        monitors_out.append({
            "name": r["name"],
            "type": r["type"],
            "url": r.get("url"),
            "host": r.get("host"),
            "port": r.get("port"),
            "status": r["status"],
            "latency_ms": r["latency_ms"],
            "message": r["message"],
            "server": r.get("server"),
            "tls": r.get("tls"),
            "host_telemetry": r.get("host_telemetry"),
            "heartbeat_age_s": r.get("heartbeat_age_s"),
            "checked_at": iso(now_ts),
            "uptime_24h": uptime_pct(samples, now_ts, UPTIME_WINDOWS["24h"]),
            "uptime_7d": uptime_pct(samples, now_ts, UPTIME_WINDOWS["7d"]),
            "uptime_30d": uptime_pct(samples, now_ts, UPTIME_WINDOWS["30d"]),
            "history": bar,
            "telemetry": telemetry,
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
# Pure HTML + org.css Renderer (Zero JavaScript)
# --------------------------------------------------------------------------

def make_bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, int(round((pct / 100.0) * width))))
    return "█" * filled + "░" * (width - filled)


def render_html(status: dict) -> str:
    monitors = status.get("monitors", [])
    incidents = status.get("incidents", [])
    overall = status.get("overall", "operational")

    if overall == "operational":
        overall_msg = "🟢 <strong>All systems operational</strong>"
    elif overall == "outage":
        overall_msg = "🔴 <strong>Major outage</strong> &mdash; all monitored services are down"
    else:
        overall_msg = "🟡 <strong>Degraded performance / partial outage</strong>"

    monitor_cards = []

    for m in monitors:
        status_str = m["status"]
        if status_str == "up":
            status_badge = "<span>🟢 Operational</span>"
        else:
            status_badge = "<strong style='color: #cb4b16;'>🔴 Offline</strong>"

        m_type = m.get("type", "http")
        if m_type == "heartbeat":
            type_icon = "🖥️"
        elif m_type == "tcp":
            type_icon = "🔌"
        else:
            type_icon = "🌐"

        if m.get("url"):
            name_html = f'{type_icon} <a href="{m["url"]}">{m["name"]}</a>'
        else:
            name_html = f'{type_icon} {m["name"]}'

        u24_val = f"{m['uptime_24h']}%" if m.get("uptime_24h") is not None else "n/a"
        u7d_val = f"{m['uptime_7d']}%" if m.get("uptime_7d") is not None else "n/a"
        u30d_val = f"{m['uptime_30d']}%" if m.get("uptime_30d") is not None else "n/a"
        t = m.get("telemetry", {})

        summary_line = f"<strong>{name_html}</strong> &middot; {status_badge}"

        if m["type"] == "heartbeat":
            ht = m.get("host_telemetry") or {}
            uname_val = ht.get("uname", "Unknown")
            uptime_val = ht.get("uptime", "Unknown")
            hostname_val = ht.get("hostname", m["name"])
            
            cpu_val = f"{ht.get('cpu_count', '--')} cores"
            load_val = f"{ht['load'][0]}, {ht['load'][1]}, {ht['load'][2]} ({cpu_val})" if ht.get("load") else "N/A"
            mem_val = f"{ht['mem']['used_gb']} GB / {ht['mem']['total_gb']} GB [{make_bar(ht['mem']['pct'])}] {ht['mem']['pct']}%" if ht.get("mem") else "N/A"
            disk_val = f"{ht['disk']['used_gb']} GB / {ht['disk']['total_gb']} GB [{make_bar(ht['disk']['pct'])}] {ht['disk']['pct']}%" if ht.get("disk") else "N/A"

            monitor_cards.append(f"""
  <details class="myborder" style="margin-bottom: 1em;">
    <summary style="cursor: pointer; padding: 4px 0;">{summary_line}</summary>
    <pre><code>=== 🖥️ Host & Kernel Information ===
Hostname:        {hostname_val}
Kernel / Uname:  {uname_val}
System Uptime:   {uptime_val}
Last Heartbeat:  {m.get('message', 'n/a')}

=== ⚡ Hardware & Resource Telemetry ===
CPU Load (1m/5m/15m): {load_val}
Memory Usage:         {mem_val}
Disk Usage (/):       {disk_val}

=== 📊 SRE Availability ===
Availability (30d):  {t.get('nines', 'n/a')}
Uptime (24h/7d/30d): {u24_val} / {u7d_val} / {u30d_val}
Current Streak:      {t.get('streak', 0)} consecutive heartbeats passed (~{t.get('streak_hours', 0)} hours)
Heartbeats Logged:   {t.get('sample_count', 0)} samples</code></pre>
  </details>""")

        else:
            spark = t.get("sparkline_24h", "────────")
            target_str = m.get("url") or f"{m.get('host')}:{m.get('port')}" or m.get("name")
            server_str = m.get("server") or "Unknown"
            tls = m.get("tls")
            tls_info_str = "None"
            if tls:
                tls_info_str = f"{tls['version']} ({tls['cipher']}) | Issuer: {tls['issuer']} | Expires: {tls['expiry']} ({tls['days_left']} days left)"

            curl_cmd = f"curl -Iv {m['url']}" if m.get("url") else f"nc -zv {m.get('host')} {m.get('port')}"

            stddev_val = t.get('stddev')
            stddev_str = f"±{stddev_val}ms" if stddev_val is not None else "--"
            current_lat = f"{m['latency_ms']} ms" if m.get("latency_ms") is not None else "--"

            monitor_cards.append(f"""
  <details class="myborder" style="margin-bottom: 1em;">
    <summary style="cursor: pointer; padding: 4px 0;">{summary_line}</summary>
    <pre><code>=== 📊 SRE & Availability ===
Availability (30d):  {t.get('nines', 'n/a')}
Uptime (24h/7d/30d): {u24_val} / {u7d_val} / {u30d_val}
Current Streak:      {t.get('streak', 0)} consecutive checks passed (~{t.get('streak_hours', 0)} hours)
Samples Logged:      {t.get('sample_count', 0)} samples

=== ⏱️ Latency Distribution (ms) ===
Current Latency:     {current_lat}
min: {t.get('min') or '--'}ms | p50: {t.get('p50') or '--'}ms | p90: {t.get('p90') or '--'}ms | p95: {t.get('p95') or '--'}ms | p99: {t.get('p99') or '--'}ms | max: {t.get('max') or '--'}ms | σ: {stddev_str}
Sparkline (24h):     {spark}

=== 📈 Latency Histogram ===
{t.get('histogram', '  No data')}

=== 🔒 TLS & Edge Fingerprint ===
HTTP Status:         {m.get('message', 'n/a')}
Server Header:       {server_str}
TLS Details:         {tls_info_str}

=== 🛠️ Diagnostic CLI ===
{curl_cmd}</code></pre>
  </details>""")

    cards_html = "\n".join(monitor_cards) if monitor_cards else "<p>No monitors configured.</p>"

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
  <h2>⚠️ Recent Incidents</h2>
  <ul>
{incidents_list}
  </ul>
"""

    updated_iso = status.get("updated", "")
    if updated_iso:
        dt = datetime.fromisoformat(updated_iso.replace("Z", "+00:00"))
    else:
        dt = datetime.now(timezone.utc)
    fallback_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    iso_str = dt.isoformat().replace("+00:00", "Z")

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
  <h1><a href="https://roars.dev">ROARS</a> Status</h1>

  <blockquote>
    {overall_msg} &mdash; checks run every 5 minutes via GitHub Actions. Click any service below for telemetry and diagnostics.
  </blockquote>

  <h2>Monitors</h2>
{cards_html}
{incidents_html}
  <hr>
  <p><small>Last updated: <time id="last-updated" datetime="{iso_str}">{fallback_str}</time></small></p>
  <script>
    const el = document.getElementById("last-updated");
    if (el) el.textContent = new Date(el.dateTime).toLocaleString();
  </script>
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
