/**
 * workflow-watchman — Cloudflare Worker
 * ---------------------------------------
 * Monitors the mercy-discord-bot GitHub Actions workflow (secure-rdp.yml) and
 * restarts it whenever it has been inactive/dead longer than its grace period.
 * This worker watches ONLY our bot repo; every other repo is deliberately not
 * in REPO_CONFIGS.
 *
 * Behaviour (mercy-discord-bot, secure-rdp.yml, branch master):
 *   - Grace: 0 — a session that goes offline (bot crash, failed run, or normal
 *     end of the keep-alive) is restarted on the very next cron tick, i.e. within
 *     ~60 s + runner boot. Extra dispatches can't fight a live session: the
 *     workflow's `concurrency: rdp-session` (cancel-in-progress: false) queues a
 *     second run behind the active one instead of overlapping it.
 *   - fastPoll: checked on EVERY cron tick (1 min), so a dead run is picked up
 *     within ~1-2 min. Only one repo is watched, so the GitHub rate-limit cost
 *     of the 1-min poll is trivial (was every 5th minute when 20 repos were
 *     monitored).
 *   - Budget: 8 restarts / 24h max (throttleMax).
 *   - `failGraceMs` remains available as a per-repo override: a shorter grace
 *     applied ONLY when the latest run failed (useful when graceMs is long).
 *
 * Restart budget (throttleMax / 24h): counted from the WATCHMAN_KV namespace
 * when the binding is configured, so it only ever counts dispatches THIS worker
 * made — manual/UI retries no longer eat the budget and lock the watchman out.
 * Without the binding it falls back to counting completed workflow_dispatch runs
 * from the API (cruder: manual retries count too). A throttled repo now posts a
 * Discord alert (once per 30 min) instead of going silent.
 */

const OWNER = "lawdachuss";

// ===== Per-repo configuration ================================================
// This worker intentionally monitors ONLY our bot repo — every other repo that
// used to be here (node-1..node-18, supabase-actions) was removed so this
// worker can't touch anything but mercy-discord-bot.
const REPO_CONFIGS = [
  // mercy-discord-bot — Discord bot with RDP access.
  // graceMs 0: restart the INSTANT a session goes offline — the moment GitHub
  // reports no active run, the next cron tick (<= 60s away) dispatches a new
  // session. Overlapping dispatches can't fight a live session: the workflow's
  // `concurrency: rdp-session` (cancel-in-progress: false) makes a second run
  // QUEUE behind the active one instead. A rapid-fail loop is still bounded by
  // throttleMax (8/24h) + the 90s per-repo dispatch cooldown.
  // fastPoll: checked on EVERY 1-min cron tick (cheap: only repo watched).
  {
    repo: "mercy-discord-bot",
    workflow: "secure-rdp.yml",
    branch: "master",
    graceMs: 0,
    throttleMax: 8,
    fastPoll: true,
  },
];

const THROTTLE_WINDOW = 24 * 60 * 60 * 1000; // 24 hours
const HEALTH_CHECK_RETRIES = 6;
const HEALTH_CHECK_INTERVAL = 30 * 1000; // 30 seconds

// ===== Per-repo dispatch/notify guards =======================================
// lastDispatchAt: prevents double-dispatch races when the schedule-queued run
// and our dispatch land in the same cron window. lastErrorAt / lastThrottleAt:
// cap Discord spam to once per 30 min per repo (a broken config was alerting
// every 5 min).
//
// NOTE: these live in module state, which is per-isolate in Workers. With the
// 1-min cron, a slow health check (up to 3 min) can overlap the next scheduled
// invocation in a different isolate, where the cooldown isn't visible. This is
// acceptable: the active-run check (queued/pending counts as active) catches a
// just-dispatched run within seconds, a duplicate with cancel-in-progress: false
// only QUEUES (never cancels a live session), and throttleMax bounds the blast
// radius. A KV/cache-backed lock would close the window entirely if ever needed.
const DISPATCH_COOLDOWN_MS = 90 * 1000;
const ERROR_NOTIFY_INTERVAL_MS = 30 * 60 * 1000;
const THROTTLE_NOTIFY_INTERVAL_MS = 30 * 60 * 1000;
const lastDispatchAt = {};
const lastErrorAt = {};
const lastThrottleAt = {};

// Restart budget is persisted in KV (binding: WATCHMAN_KV) as a JSON array of
// dispatch timestamps (ms). KV is eventually consistent (~60s across edges) —
// fine here: the per-repo dispatch cooldown (90s) plus the health check already
// serialize dispatches far more tightly than that.
const RESTARTS_TTL_SEC = 24 * 60 * 60;

// ===== Helpers ===============================================================

function ghHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    "User-Agent": "workflow-watchman/2.0",
    Accept: "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

// GitHub secondary rate limits return HTTP 429 with a Retry-After header.
// Wrap every API call so short 429s back off and retry instead of failing
// the whole repo scan/restart. Primary rate limits (403 + ratelimit headers)
// are not retried — those would need to wait up to an hour.
const GH_MAX_ATTEMPTS = 3;
// If GitHub asks us to wait longer than this, don't hammer the endpoint —
// return the 429 and let the next cron cycle (5 min) pick the repo up.
const GH_BACKOFF_BUDGET_SEC = 90;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Retry-After is seconds ("60") or an HTTP-date; fall back to 5s.
function retryAfterSeconds(resp) {
  const ra = resp.headers.get("Retry-After");
  if (!ra) return 5;
  const secs = Number(ra);
  if (Number.isFinite(secs)) return Math.max(secs, 1);
  const dateMs = Date.parse(ra);
  if (Number.isFinite(dateMs)) {
    return Math.max(Math.ceil((dateMs - Date.now()) / 1000), 1);
  }
  return 5;
}

async function ghFetch(url, headers, init = {}) {
  for (let attempt = 1; attempt <= GH_MAX_ATTEMPTS; attempt++) {
    const resp = await fetch(url, { ...init, headers: { ...headers, ...(init.headers || {}) } });
    if (resp.status !== 429 || attempt === GH_MAX_ATTEMPTS) return resp;
    const wait = retryAfterSeconds(resp);
    if (wait > GH_BACKOFF_BUDGET_SEC) {
      console.log(
        `[gh] 429 on ${url} — Retry-After ${wait}s exceeds budget ${GH_BACKOFF_BUDGET_SEC}s, deferring to next cycle`
      );
      return resp;
    }
    console.log(`[gh] 429 on ${url} — backing off ${wait}s (attempt ${attempt}/${GH_MAX_ATTEMPTS})`);
    await sleep(wait * 1000);
  }
}

async function githubErrorDetail(resp) {
  try {
    const text = await resp.text();
    if (!text) return "";
    try {
      const data = JSON.parse(text);
      const detail = data.message || data.error || (Array.isArray(data.errors) ? data.errors[0]?.message : "");
      return String(detail || text).replace(/\s+/g, " ").slice(0, 300);
    } catch {
      return text.replace(/\s+/g, " ").slice(0, 300);
    }
  } catch {
    return "";
  }
}

function fmtDuration(ms) {
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

// ===== Discord notifications =================================================

async function sendDiscordAlert(env, repo, summary) {
  const url = env.DISCORD_WEBHOOK_URL;
  if (!url) return;
  const fields = [];
  if (summary.deadDuration) fields.push({ name: "Dead for", value: summary.deadDuration, inline: true });
  if (summary.lastConclusion) fields.push({ name: "Last conclusion", value: summary.lastConclusion, inline: true });
  fields.push({ name: "Restarts today", value: String(summary.restartsToday || 0), inline: true });
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      embeds: [{
        title: `🔄 ${repo} restarted`,
        color: 15105570,
        description: summary.reason || "",
        fields,
        footer: { text: "workflow-watchman" },
        timestamp: new Date().toISOString(),
      }],
    }),
  });
  if (!resp.ok) console.error(`[notify] Discord webhook returned ${resp.status}`);
}

async function sendDiscordError(env, repo, message) {
  const url = env.DISCORD_WEBHOOK_URL;
  if (!url) return;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      embeds: [{
        title: `⚠️ ${repo} error`,
        color: 15158332,
        description: message,
        footer: { text: "workflow-watchman" },
        timestamp: new Date().toISOString(),
      }],
    }),
  });
}

// Throttled repos used to go completely silent (restarts paused for hours with
// no signal at all). Announce the lockout once per 30 min per repo, including
// when the budget frees up again.
async function maybeNotifyThrottled(env, state) {
  const repo = state.name;
  const now = Date.now();
  if (now - (lastThrottleAt[repo] || 0) < THROTTLE_NOTIFY_INTERVAL_MS) return;
  lastThrottleAt[repo] = now;
  const url = env.DISCORD_WEBHOOK_URL;
  if (!url) return;
  const fields = [
    { name: "Restarts today", value: String(state.restarts || 0), inline: true },
    { name: "Budget", value: `${state.restarts || 0} / ${state.budget || 8} in 24h`, inline: true },
  ];
  if (state.resumeAt) {
    fields.push({ name: "Budget frees up (UTC)", value: state.resumeAt.replace("T", " ").slice(0, 16), inline: true });
  }
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      embeds: [{
        title: `⏸️ ${repo} restart paused`,
        color: 16776960,
        description: `${state.reason || "Throttled"}\nAutomatic restarts are paused until the 24h budget frees up. Manual runs are unaffected.`,
        fields,
        footer: { text: "workflow-watchman" },
        timestamp: new Date().toISOString(),
      }],
    }),
  });
  if (!resp.ok) console.error(`[notify] Discord webhook returned ${resp.status}`);
}

// ===== Dashboard / Metrics ===================================================

// Result cache for the public dashboard (60s TTL): each page view calls the
// GitHub API for EVERY repo, so a public worker URL without caching would let
// anyone exhaust the token's 5000/hr rate limit (DoS on restarts).
const DASHBOARD_CACHE_TTL = 60 * 1000;
let dashboardCache = null;
let dashboardCacheAt = 0;

async function fetchAllStatusCached(env) {
  const now = Date.now();
  if (dashboardCache && now - dashboardCacheAt < DASHBOARD_CACHE_TTL) {
    return dashboardCache;
  }
  dashboardCache = await fetchAllStatus(env);
  dashboardCacheAt = now;
  return dashboardCache;
}

function isDashboardAuthed(request, env) {
  const secret = env.DASHBOARD_TOKEN;
  if (!secret) return true; // no token configured -> open (cache still protects rate limit)
  const provided = request.headers.get("x-watchman-token") ||
    new URL(request.url).searchParams.get("token") || "";
  return provided === secret;
}

const STATUS_ICON = {
  in_progress: "🟢",
  queued: "🟡",
  pending: "🟡",
  completed: "⚪",
};

function renderDashboard(repos) {
  const rows = repos
    .map((r) => {
      const icon = STATUS_ICON[r.status] || "⚪";
      let age = "—";
      if (r.lastRun) {
        const ms = Date.now() - new Date(r.lastRun).getTime();
        if (ms < 60000) age = "<1m";
        else if (ms < 3600000) age = `${Math.floor(ms / 60000)}m`;
        else age = `${Math.floor(ms / 3600000)}h ${Math.floor((ms % 3600000) / 60000)}m`;
      }
      return `<tr>
        <td><strong>${r.name}</strong></td>
        <td>${r.throttled ? "⏸ throttled" : `${icon} ${r.status || "no runs"}`}</td>
        <td>${r.lastRun ? new Date(r.lastRun).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "—"}</td>
        <td>${age}</td>
        <td>${r.restarts ?? 0}</td>
      </tr>`;
    })
    .join("\n");
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>workflow-watchman</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#0b0d11; color:#cdd0d4; padding:40px 20px; }
h1 { font-size:24px; font-weight:600; margin-bottom:4px; color:#e5ebf0; }
.subtitle { color:#6b7280; font-size:14px; margin-bottom:24px; }
table { width:100%; border-collapse:collapse; background:#13161b; border-radius:8px; overflow:hidden; }
th { text-align:left; padding:12px 16px; font-size:12px; text-transform:uppercase; letter-spacing:0.05em; color:#9ca3af; border-bottom:1px solid #1f2937; }
td { padding:12px 16px; border-bottom:1px solid #1a1f26; font-size:14px; }
tr:last-child td { border-bottom:none; }
tr:hover td { background:#1a1f26; }
.badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }
.badge-ok { background:#065f46; color:#6ee7b7; }
.badge-warn { background:#78350f; color:#fcd34d; }
.badge-err { background:#7f1d1d; color:#fca5a5; }
.meta { margin-top:16px; font-size:12px; color:#6b7280; }
.meta span { margin-right:16px; }
a { color:#3b82f6; }
</style>
</head>
<body>
<h1>🛡️ workflow-watchman</h1>
<p class="subtitle">Monitoring ${repos.length} repo • cron */1 * * * * (mercy-discord-bot, fast-polled)</p>
<table>
<thead><tr><th>Repo</th><th>Status</th><th>Last Run</th><th>Age</th><th>Restarts</th></tr></thead>
<tbody>${rows}</tbody>
</table>
<p class="meta">
<span>Last check: ${new Date().toISOString().replace("T", " ").slice(0, 19)} UTC</span>
<span><a href="/__metrics">JSON metrics</a></span>
</p>
</body>
</html>`;
}

function renderMetrics(repos) {
  return {
    checkedAt: new Date().toISOString(),
    totalRepos: repos.length,
    running: repos.filter((r) => r.status === "in_progress").length,
    idle: repos.filter((r) => !r.status || r.status === "completed").length,
    repos: Object.fromEntries(
      repos.map((r) => [
        r.name,
        {
          status: r.status || "no_runs",
          lastRun: r.lastRun,
          lastConclusion: r.lastConclusion,
          restarts: r.restarts ?? 0,
          throttled: !!r.throttled,
          resumeAt: r.resumeAt || null,
        },
      ])
    ),
  };
}

// ===== Core logic ============================================================

// Restart budget: timestamps (ms) of WATCHMAN restarts dispatched for this repo
// in the last 24h.
//
// With the WATCHMAN_KV binding this reads our own dispatch log, so only
// restarts we made count — a user hammering "Run workflow" in the UI while
// debugging a broken run no longer eats the budget and locks the watchman out
// for the rest of the day (that is exactly what happened on Oct 7: 11 manual
// retries in 40 min silenced auto-restart for ~16h).
//
// Without the binding we fall back to counting completed workflow_dispatch runs
// from the already-fetched runs payload — no extra API call, but manual
// dispatches count too (the pre-KV behaviour).
async function loadWatchmanRestarts(env, config, runs) {
  const kv = env.WATCHMAN_KV;
  if (kv) {
    const key = `restarts:${config.repo}`;
    const stored = (await kv.get(key, "json")) || [];
    const cutoff = Date.now() - THROTTLE_WINDOW;
    const live = stored.filter((t) => t > cutoff);
    if (live.length !== stored.length) {
      await kv.put(key, JSON.stringify(live), { expirationTtl: RESTARTS_TTL_SEC });
    }
    return live;
  }
  const cutoff = Date.now() - THROTTLE_WINDOW;
  return (runs || [])
    .filter((r) => {
      if (r.status !== "completed" || r.event !== "workflow_dispatch") return false;
      if (r.conclusion === "cancelled") return false;
      return new Date(r.created_at).getTime() > cutoff;
    })
    .map((r) => new Date(r.created_at).getTime());
}

// Called right after a successful dispatch. Read-modify-write on KV can race a
// concurrent isolate, but DISPATCH_COOLDOWN_MS (90s) + the active-run check
// keep it to one dispatch per repo per window, and worst case loses/duplicates
// one timestamp in a counter that tolerates +/-1.
async function recordWatchmanRestart(env, config) {
  const kv = env.WATCHMAN_KV;
  if (!kv) return;
  const key = `restarts:${config.repo}`;
  const stored = (await kv.get(key, "json")) || [];
  const cutoff = Date.now() - THROTTLE_WINDOW;
  const live = stored.filter((t) => t > cutoff);
  live.push(Date.now());
  await kv.put(key, JSON.stringify(live), { expirationTtl: RESTARTS_TTL_SEC });
}

async function evaluateRepo(config, headers, env) {
  const { repo, workflow, graceMs, throttleMax } = config;
  const resp = await ghFetch(
    `https://api.github.com/repos/${OWNER}/${repo}/actions/workflows/${workflow}/runs?per_page=30`,
    headers
  );
  if (!resp.ok) throw new Error(`runs API: ${resp.status}`);

  const { workflow_runs: runs } = await resp.json();
  const state = {
    name: repo,
    needsRestart: false,
    status: null,
    lastRun: null,
    lastConclusion: null,
    deadDuration: null,
    restarts: 0,
    budget: throttleMax,
  };

  // No runs ever → needs restart
  if (!runs || runs.length === 0) {
    state.needsRestart = true;
    state.reason = "No runs ever";
    return state;
  }

  // Active run → no restart needed
  const activeRun = runs.find(
    (r) => r.status === "in_progress" || r.status === "queued" || r.status === "pending"
  );
  if (activeRun) {
    state.status = activeRun.status;
    state.lastRun = activeRun.run_started_at || activeRun.created_at;
    state.lastConclusion = null;
    return state;
  }

  // Latest completed run. Age is measured from updated_at (approximately completion
  // time), not run_started_at - for a 5h30m session those differ by hours, and the
  // grace window is meant to say "how long since the session ENDED".
  const latest = runs[0];
  state.status = latest.status;
  state.lastRun = latest.run_started_at || latest.created_at;
  state.lastConclusion = latest.conclusion;
  const endedAt = new Date(latest.updated_at || latest.created_at).getTime();
  const age = Number.isFinite(endedAt) ? Date.now() - endedAt : Infinity; // null -> treat as very old

  // Restart budget first (before the grace check) so the dashboard shows an
  // accurate restarts-today count even while the repo is inside its grace window.
  const restarts = await loadWatchmanRestarts(env, config, runs);
  state.restarts = restarts.length;

  // Grace period. A failed run is retried on the short failGraceMs window
  // (when configured) — the session never got off the ground, so there is no
  // live session to protect with a long grace. Successful runs keep graceMs.
  const failed = latest.status === "completed" && latest.conclusion !== "success";
  const grace = failed && config.failGraceMs != null ? config.failGraceMs : graceMs;
  if (age < grace) return state;

  if (restarts.length >= throttleMax) {
    const sorted = restarts.slice().sort((a, b) => a - b);
    state.throttled = true;
    state.resumeAt = new Date(sorted[restarts.length - throttleMax] + THROTTLE_WINDOW).toISOString();
    state.reason = `Throttled (${restarts.length} watchman restarts in 24h)`;
    return state;
  }

  // Needs restart
  state.needsRestart = true;
  state.deadDuration = fmtDuration(age);
  state.reason = latest.conclusion
    ? `Last run ${latest.conclusion} at ${latest.run_started_at}`
    : "No recent activity";
  return state;
}

async function dispatchWorkflow(config, headers) {
  const { repo, workflow, branch } = config;
  const url = `https://api.github.com/repos/${OWNER}/${repo}/actions/workflows/${workflow}/dispatches`;
  const send = (payload) =>
    ghFetch(url, headers, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

  let resp = await send({ ref: branch, inputs: { triggered_by: "workflow-watchman" } });
  let detail = resp.ok ? "" : await githubErrorDetail(resp);
  if (resp.status === 422 && /input|workflow_dispatch/i.test(detail)) {
    console.log(`[${repo}] retrying dispatch without inputs — ${detail}`);
    resp = await send({ ref: branch });
    detail = resp.ok ? "" : await githubErrorDetail(resp);
  }
  return { ok: resp.ok, status: resp.status, detail };
}

async function executeRestart(config, state, headers, env) {
  const { repo } = config;
  const now = Date.now();
  if (now - (lastDispatchAt[repo] || 0) < DISPATCH_COOLDOWN_MS) {
    console.log(`[${repo}] skipping dispatch (cooldown)`);
    return;
  }
  console.log(`[${repo}] restarting — ${state.reason}`);

  const dispatch = await dispatchWorkflow(config, headers);
  if (!dispatch.ok) {
    throw new Error(`dispatch API: ${dispatch.status}${dispatch.detail ? ` — ${dispatch.detail}` : ""}`);
  }

  const dispatchedAt = Date.now();
  lastDispatchAt[repo] = dispatchedAt;
  await recordWatchmanRestart(env, config);
  console.log(`[${repo}] dispatched`);
  await sendDiscordAlert(env, repo, {
    reason: state.reason,
    deadDuration: state.deadDuration,
    lastConclusion: state.lastConclusion,
    restartsToday: state.restarts + 1,
  });

  const healthOk = await healthCheck(config, headers, dispatchedAt);
  if (!healthOk) {
    await sendDiscordError(env, repo, "Health check failed — new run did not start within 3 min");
  } else {
    console.log(`[${repo}] health check passed`);
  }
}

async function healthCheck(config, headers, dispatchedAt) {
  const { repo, workflow } = config;
  const createdAfterDispatch = dispatchedAt - 10000;
  for (let i = 0; i < HEALTH_CHECK_RETRIES; i++) {
    await new Promise((r) => setTimeout(r, HEALTH_CHECK_INTERVAL));
    const resp = await ghFetch(
      `https://api.github.com/repos/${OWNER}/${repo}/actions/workflows/${workflow}/runs?per_page=1`,
      headers
    );
    if (!resp.ok) continue;
    const { workflow_runs: runs } = await resp.json();
    if (!runs || runs.length === 0) continue;
    const latest = runs[0];
    const createdAt = new Date(latest.created_at).getTime();
    if (!Number.isFinite(createdAt) || createdAt < createdAfterDispatch) continue;
    const isActive = latest.status === "in_progress" || latest.status === "queued" || latest.status === "pending";
    const isSuccessful = latest.status === "completed" && latest.conclusion === "success";
    if (isActive || isSuccessful) {
      console.log(`[${repo}] health check OK — run ${latest.id} ${latest.status}${latest.conclusion ? " (" + latest.conclusion + ")" : ""}`);
      return true;
    }
  }
  return false;
}

async function fetchAllStatus(env) {
  const token = env.GITHUB_TOKEN;
  if (!token) return [];
  const headers = ghHeaders(token);
  const results = new Array(REPO_CONFIGS.length);
  await runWithConcurrency(REPO_CONFIGS, 4, async (config, i) => {
    try {
      results[i] = await evaluateRepo(config, headers, env);
    } catch {
      results[i] = { name: config.repo, status: "error" };
    }
  });
  return results;
}

// ===== Concurrency =====================================================
// Run configs with at most `limit` in flight. GitHub secondary rate limits
// can 429 a full 15-way burst, so restart work is capped at 4 at a time.
async function runWithConcurrency(configs, limit, fn) {
  let index = 0;
  const workers = Array.from(
    { length: Math.min(limit, configs.length) },
    async () => {
      while (index < configs.length) {
        const i = index++;
        await fn(configs[i], i);
      }
    }
  );
  await Promise.allSettled(workers);
}

// ===== Worker handlers =======================================================

export default {
  async scheduled(event, env, ctx) {
    const token = env.GITHUB_TOKEN;
    if (!token) {
      console.error("GITHUB_TOKEN not set");
      return;
    }
    const headers = ghHeaders(token);
    // Fast-poll gate: the cron fires every minute, but only repos flagged
    // fastPoll are checked on EVERY tick; everything else waits for the 5th
    // minute to keep GitHub rate-limit and free-plan CPU usage low.
    // mercy-discord-bot is fastPolled, so a dead run is spotted in ~1-2 min.
    const isFiveMinuteTick = Math.floor(Date.now() / 60000) % 5 === 0;
    await runWithConcurrency(REPO_CONFIGS, 4, async (config) => {
      if (!config.fastPoll && !isFiveMinuteTick) return;
      try {
        const state = await evaluateRepo(config, headers, env);
        if (state.throttled) {
          // Budget exhausted: don't dispatch, but don't go silent either.
          console.log(`[${config.repo}] throttled until ${state.resumeAt || "budget frees"}`);
          await maybeNotifyThrottled(env, state);
        } else if (state.needsRestart) {
          await executeRestart(config, state, headers, env);
        }
      } catch (err) {
        console.error(`[${config.repo}] error: ${err.message}`);
        const now = Date.now();
        if (now - (lastErrorAt[config.repo] || 0) >= ERROR_NOTIFY_INTERVAL_MS) {
          lastErrorAt[config.repo] = now;
          await sendDiscordError(env, config.repo, err.message);
        }
      }
    });
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/__health") return new Response("OK", { status: 200 });
    if (url.pathname === "/__metrics" || url.pathname === "/" || url.pathname === "") {
      if (!isDashboardAuthed(request, env)) {
        return new Response("Unauthorized", { status: 401 });
      }
    }
    if (url.pathname === "/__metrics") {
      const data = await fetchAllStatusCached(env);
      return new Response(JSON.stringify(renderMetrics(data), null, 2), {
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.pathname === "/" || url.pathname === "") {
      const data = await fetchAllStatusCached(env);
      return new Response(renderDashboard(data), {
        headers: { "Content-Type": "text/html" },
      });
    }
    return new Response("Not found", { status: 404 });
  },
};
