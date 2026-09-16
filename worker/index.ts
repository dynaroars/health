export interface Env {
  CLOUDFLARE_API_TOKEN?: string;
  CLOUDFLARE_ZONE_ID?: string;
  STATS_HOSTNAMES?: string;
  STATS_KV?: KVNamespace;
}

interface KVNamespace {
  get<T>(key: string, type: 'json'): Promise<T | null>;
  put(key: string, value: string): Promise<void>;
}

interface DailyStat { date: string; requests: number; pageViews: number; visits: number; }
interface AnalyticsRow {
  count?: number;
  sum?: { visits?: number };
  dimensions?: { clientCountryName?: string; clientRequestPath?: string };
}
type ZoneAnalytics = Record<string, AnalyticsRow[] | undefined>;

const HISTORY_PREFIX = 'health-stats-v1:';
const LOOKBACK_DAYS = 7;
const MAX_HISTORY_DAYS = 30;

function hosts(env: Env): string[] {
  return [...new Set((env.STATS_HOSTNAMES || '').split(',').map(value => value.trim().toLowerCase()).filter(Boolean))];
}

function datesEndingToday(count: number): string[] {
  const today = new Date();
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(today);
    date.setUTCDate(date.getUTCDate() - count + index + 1);
    return date.toISOString().slice(0, 10);
  });
}

function sum(rows: DailyStat[]): Omit<DailyStat, 'date'> {
  return rows.reduce((total, row) => ({ requests: total.requests + row.requests, pageViews: total.pageViews + row.pageViews, visits: total.visits + row.visits }), { requests: 0, pageViews: 0, visits: 0 });
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 ? ordered[middle] : Math.round((ordered[middle - 1] + ordered[middle]) / 2);
}

function flag(code: string): string {
  if (!/^[A-Z]{2}$/.test(code)) return '🌐';
  return String.fromCodePoint(...[...code].map(letter => letter.charCodeAt(0) - 65 + 0x1F1E6));
}

function countryName(code: string): string {
  try { return new Intl.DisplayNames(['en'], { type: 'region' }).of(code) || code; } catch { return code; }
}

function query(dates: string[]): string {
  const variables = dates.map((_, index) => `$date${index}: Date!`).join(', ');
  const groups = dates.map((_, index) => `
    traffic${index}: httpRequestsAdaptiveGroups(limit: 1, filter: { date: $date${index}, clientRequestHTTPHost: $hostname, requestSource: "eyeball" }) { count }
    pages${index}: httpRequestsAdaptiveGroups(limit: 1, filter: { date: $date${index}, clientRequestHTTPHost: $hostname, requestSource: "eyeball", edgeResponseContentTypeName: "html", edgeResponseStatus_geq: 200, edgeResponseStatus_lt: 400 }) { count sum { visits } }
    countries${index}: httpRequestsAdaptiveGroups(limit: 250, filter: { date: $date${index}, clientRequestHTTPHost: $hostname, requestSource: "eyeball", edgeResponseContentTypeName: "html", edgeResponseStatus_geq: 200, edgeResponseStatus_lt: 400 }) { sum { visits } dimensions { clientCountryName } }
    pagesByPath${index}: httpRequestsAdaptiveGroups(limit: 250, filter: { date: $date${index}, clientRequestHTTPHost: $hostname, requestSource: "eyeball", edgeResponseContentTypeName: "html", edgeResponseStatus_geq: 200, edgeResponseStatus_lt: 400 }) { count dimensions { clientRequestPath } }`).join('');
  return `query Stats($zoneTag: String!, $hostname: String!, ${variables}) { viewer { zones(filter: { zoneTag: $zoneTag }) {${groups} } } }`;
}

async function stats(env: Env, hostname: string, persist = false) {
  if (!env.CLOUDFLARE_API_TOKEN || !env.CLOUDFLARE_ZONE_ID) throw new Error('Analytics credentials are not configured.');
  const sourceDates = datesEndingToday(LOOKBACK_DAYS);
  const variables: Record<string, string> = { zoneTag: env.CLOUDFLARE_ZONE_ID, hostname };
  sourceDates.forEach((date, index) => { variables[`date${index}`] = date; });
  const response = await fetch('https://api.cloudflare.com/client/v4/graphql', { method: 'POST', headers: { Authorization: `Bearer ${env.CLOUDFLARE_API_TOKEN}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ query: query(sourceDates), variables }) });
  if (!response.ok) throw new Error(`Cloudflare GraphQL returned HTTP ${response.status}.`);
  const body = await response.json() as { data?: { viewer?: { zones?: ZoneAnalytics[] } }; errors?: { message: string }[] };
  if (body.errors?.length) throw new Error(body.errors.map(error => error.message).join('; '));
  const zone = body.data?.viewer?.zones?.[0];
  if (!zone) throw new Error('Cloudflare returned no analytics zone data.');
  const fresh = sourceDates.map((date, index) => ({ date, requests: zone[`traffic${index}`]?.[0]?.count || 0, pageViews: zone[`pages${index}`]?.[0]?.count || 0, visits: zone[`pages${index}`]?.[0]?.sum?.visits || 0 }));
  const key = HISTORY_PREFIX + hostname;
  const archived = env.STATS_KV ? (await env.STATS_KV.get<DailyStat[]>(key, 'json') || []) : [];
  const earliest = datesEndingToday(MAX_HISTORY_DAYS)[0];
  const daily = [...new Map([...archived, ...fresh].map(row => [row.date, row])).values()].filter(row => row.date >= earliest).sort((a, b) => a.date.localeCompare(b.date));
  if (persist && env.STATS_KV) await env.STATS_KV.put(key, JSON.stringify(daily));
  const countries = new Map<string, number>();
  const pages = new Map<string, number>();
  sourceDates.forEach((_, index) => {
    (zone[`countries${index}`] || []).forEach(row => { const code = (row.dimensions?.clientCountryName || 'XX').toUpperCase(); countries.set(code, (countries.get(code) || 0) + (row.sum?.visits || 0)); });
    (zone[`pagesByPath${index}`] || []).forEach(row => { const path = row.dimensions?.clientRequestPath || '/'; pages.set(path, (pages.get(path) || 0) + (row.count || 0)); });
  });
  const last7 = daily.slice(-7);
  const countryRows = [...countries].map(([code, visits]) => ({ code, name: countryName(code), flag: flag(code), count: visits, visits })).filter(row => row.visits).sort((a, b) => b.visits - a.visits);
  const totalCountries = countryRows.reduce((total, row) => total + row.visits, 0);
  countryRows.forEach(row => Object.assign(row, { pct: totalCountries ? Math.round(row.visits / totalCountries * 1000) / 10 : 0 }));
  return { generatedAt: new Date().toISOString(), coverage: { last7Days: last7.length, last30Days: daily.length }, today: daily.at(-1) || fresh.at(-1), last7Days: sum(last7), last30Days: sum(daily), baseline: { medianVisitsPerDay: median(last7.map(row => row.visits)) }, countriesCount: countryRows.length, topCountries: countryRows, topPages: [...pages].map(([path, count]) => ({ path, label: path, count })).sort((a, b) => b.count - a.count), daily };
}

const headers = { 'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'public, max-age=600' };

export default {
  async fetch(request: Request, env: Env, ctx: { waitUntil(promise: Promise<unknown>): void }) {
    const url = new URL(request.url);
    if (request.method === 'OPTIONS') return new Response(null, { headers });
    if (request.method !== 'GET') return new Response(JSON.stringify({ error: 'Method Not Allowed' }), { status: 405, headers });
    if (url.pathname === '/api/health') return new Response(JSON.stringify({ status: 'ok', worker: 'roars-health-stats-api' }), { headers });
    if (url.pathname !== '/api/stats') return new Response(JSON.stringify({ error: 'Not Found' }), { status: 404, headers });
    const hostname = (url.searchParams.get('q') || '').toLowerCase();
    if (!hosts(env).includes(hostname)) return new Response(JSON.stringify({ error: 'Unsupported hostname' }), { status: 400, headers });
    try { return new Response(JSON.stringify(await stats(env, hostname)), { headers }); }
    catch (error) { return new Response(JSON.stringify({ error: 'Visitor statistics temporarily unavailable', message: error instanceof Error ? error.message : String(error) }), { status: 503, headers: { ...headers, 'Cache-Control': 'no-store' } }); }
  },
  async scheduled(_event: unknown, env: Env, ctx: { waitUntil(promise: Promise<unknown>): void }) {
    ctx.waitUntil(Promise.all(hosts(env).map(hostname => stats(env, hostname, true))).then(() => undefined));
  },
};
