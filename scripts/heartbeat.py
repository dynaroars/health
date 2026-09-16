#!/usr/bin/env python3
"""Push a rich liveness heartbeat and system telemetry for this machine to the repo's
`data` branch via the GitHub Contents API.

Usage:
    python scripts/heartbeat.py <slug>

Example crontab entry (every 5 minutes):
    */5 * * * * GH_TOKEN=github_pat_xxx REPO=dynaroars/health /usr/bin/python3 /path/to/scripts/heartbeat.py prime
"""
from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BRANCH = "data"
DEFAULT_REPO = "dynaroars/health"


def get_system_telemetry() -> dict:
    uname_str = " ".join(platform.uname())
    hostname = platform.node()
    cpu_count = os.cpu_count() or 1
    
    try:
        load1, load5, load15 = os.getloadavg()
        load_avg = [round(load1, 2), round(load5, 2), round(load15, 2)]
    except Exception:
        load_avg = None

    disk = {}
    try:
        total_d, used_d, free_d = shutil.disk_usage("/")
        disk = {
            "total_gb": round(total_d / (1024**3), 1),
            "used_gb": round(used_d / (1024**3), 1),
            "free_gb": round(free_d / (1024**3), 1),
            "pct": round((used_d / total_d) * 100, 1) if total_d else 0,
        }
    except Exception:
        pass

    mem = {}
    if os.path.exists("/proc/meminfo"):
        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_kb = meminfo.get("MemTotal", 0)
            avail_kb = meminfo.get("MemAvailable", 0)
            used_kb = total_kb - avail_kb
            mem = {
                "total_gb": round(total_kb / (1024**2), 1),
                "used_gb": round(used_kb / (1024**2), 1),
                "free_gb": round(avail_kb / (1024**2), 1),
                "pct": round((used_kb / total_kb) * 100, 1) if total_kb else 0,
            }
        except Exception:
            pass

    uptime_str = None
    if os.path.exists("/proc/uptime"):
        try:
            with open("/proc/uptime") as f:
                secs = float(f.read().split()[0])
                days = int(secs // 86400)
                hours = int((secs % 86400) // 3600)
                mins = int((secs % 3600) // 60)
                uptime_str = f"{days}d {hours}h {mins}m"
        except Exception:
            pass

    vpn = {}
    net_dir = "/sys/class/net"
    if os.path.isdir(net_dir):
        try:
            for iface in sorted(os.listdir(net_dir)):
                if iface.startswith(("wg", "tun", "tailscale", "tap")):
                    rx_bytes = 0
                    tx_bytes = 0
                    stats_dir = os.path.join(net_dir, iface, "statistics")
                    if os.path.isdir(stats_dir):
                        try:
                            with open(os.path.join(stats_dir, "rx_bytes")) as f:
                                rx_bytes = int(f.read().strip())
                            with open(os.path.join(stats_dir, "tx_bytes")) as f:
                                tx_bytes = int(f.read().strip())
                        except Exception:
                            pass
                    
                    ip_addr = None
                    try:
                        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show", iface], text=True, timeout=2)
                        parts = out.split()
                        if len(parts) >= 4:
                            ip_addr = parts[3]
                    except Exception:
                        pass
                    
                    vpn[iface] = {
                        "ip": ip_addr,
                        "rx_gb": round(rx_bytes / (1024**3), 2),
                        "tx_gb": round(tx_bytes / (1024**3), 2),
                        "status": "active",
                    }
        except Exception:
            pass

    return {
        "uname": uname_str,
        "hostname": hostname,
        "uptime": uptime_str,
        "load": load_avg,
        "cpu_count": cpu_count,
        "disk": disk,
        "mem": mem,
        "vpn": vpn,
    }


def get_auth_token() -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    # Try gh cli if installed
    try:
        res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_repo() -> str:
    repo = os.environ.get("REPO")
    if repo:
        return repo
    try:
        res = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            url = res.stdout.strip()
            if "github.com:" in url:
                return url.split("github.com:")[1].removesuffix(".git")
            elif "github.com/" in url:
                return url.split("github.com/")[1].removesuffix(".git")
    except Exception:
        pass
    return DEFAULT_REPO


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: heartbeat.py <slug>")
    slug = sys.argv[1]

    token = get_auth_token()
    repo = get_repo()
    if not token or not repo:
        sys.exit("GH_TOKEN (or gh CLI auth) and REPO are required")

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

    telemetry = get_system_telemetry()
    payload_body = {
        "last_seen": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "telemetry": telemetry,
    }
    body_bytes = json.dumps(payload_body, indent=2).encode()
    payload = {
        "message": f"heartbeat: {slug}",
        "content": base64.b64encode(body_bytes).decode(),
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
    print(f"heartbeat pushed for {slug} (load: {telemetry.get('load')}, uptime: {telemetry.get('uptime')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
