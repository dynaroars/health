const DATA_URL = "data/status.json";
const REFRESH_MS = 60_000;

async function fetchStatus() {
  const res = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`fetch failed: ${res.status}`);
  return res.json();
}

function fmtTime(iso) {
  if (!iso) return "n/a";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtLatency(ms) {
  return ms === null || ms === undefined ? "--" : `${ms} ms`;
}

function fmtUptime(pct) {
  return pct === null || pct === undefined ? "n/a" : `${pct.toFixed(2)}%`;
}

function renderBar(history) {
  const ticks = history
    .map((v) => `<span class="tick ${v === 1 ? "up" : "down"}"></span>`)
    .join("");
  return `<div class="bar">${ticks}</div>`;
}

function renderMonitor(m) {
  const label = m.status === "up" ? "Operational" : "Unavailable";
  return `
    <div class="monitor ${m.status}">
      <div class="monitor-top">
        <span class="monitor-name">${m.name}</span>
        <span class="monitor-status">
          <span><span class="dot"></span><span class="status-label">${label}</span></span>
          <span class="latency">${fmtLatency(m.latency_ms)}</span>
        </span>
      </div>
      ${renderBar(m.history)}
      <div class="monitor-bottom">
        <span>uptime 24h ${fmtUptime(m.uptime_24h)} &middot; 7d ${fmtUptime(m.uptime_7d)} &middot; 30d ${fmtUptime(m.uptime_30d)}</span>
        <span>checked ${fmtTime(m.checked_at)}</span>
      </div>
      <div class="monitor-message">${m.message ?? ""}</div>
    </div>
  `;
}

function renderIncident(inc) {
  const ongoing = inc.end === null;
  const range = ongoing
    ? `since ${fmtTime(inc.start)} (ongoing)`
    : `${fmtTime(inc.start)} &ndash; ${fmtTime(inc.end)} (${inc.duration_min} min)`;
  return `<li class="${ongoing ? "ongoing" : ""}"><span class="incident-monitor">${inc.monitor}</span> &mdash; ${range}</li>`;
}

const OVERALL_TEXT = {
  operational: "All systems operational",
  degraded: "Partial outage",
  outage: "Major outage",
};

function render(data) {
  const overallEl = document.getElementById("overall");
  overallEl.className = `overall ${data.overall}`;
  document.getElementById("overall-text").textContent =
    OVERALL_TEXT[data.overall] ?? data.overall;

  const monitorsEl = document.getElementById("monitors");
  monitorsEl.innerHTML = data.monitors.length
    ? data.monitors.map(renderMonitor).join("")
    : '<p class="empty">No monitors configured.</p>';

  const incidentsSection = document.getElementById("incidents-section");
  const incidentsEl = document.getElementById("incidents");
  if (data.incidents && data.incidents.length) {
    incidentsSection.hidden = false;
    incidentsEl.innerHTML = data.incidents.map(renderIncident).join("");
  } else {
    incidentsSection.hidden = true;
  }

  document.getElementById("updated").textContent = `updated ${fmtTime(data.updated)}`;
}

async function refresh() {
  try {
    render(await fetchStatus());
  } catch (err) {
    document.getElementById("overall-text").textContent = "Status unavailable";
    document.getElementById("monitors").innerHTML =
      `<p class="empty">Could not load status data (${err.message}).</p>`;
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
