import io, os, json, sys, time
import quickjs

HERE = os.path.dirname(os.path.abspath(__file__))
src = io.open(os.path.join(HERE, "src", "index.js"), encoding="utf-8").read()
src = src.replace("export default", "var __worker_default =", 1)

TEST = r"""
// ===== mocks =====
globalThis.__runs = [];
globalThis.fetch = async function () {
  return {
    ok: true,
    status: 200,
    headers: { get: function () { return null; } },
    json: async function () { return { workflow_runs: globalThis.__runs }; },
    text: async function () { return ""; },
  };
};

const M = 60 * 1000, H = 3600 * 1000;
const iso = (ms) => new Date(ms).toISOString();
const mercy = REPO_CONFIGS.find((c) => c.repo === "mercy-discord-bot");
const headers = { Authorization: "x" };

function mkRun(over) {
  const t = Date.now() - 30 * M;
  return Object.assign({
    status: "completed", conclusion: "success", event: "schedule",
    created_at: iso(t), updated_at: iso(t), run_started_at: iso(t),
  }, over);
}
function kvMock(entries) {
  const store = { ["restarts:" + mercy.repo]: JSON.stringify(entries || []) };
  return {
    get: async (k, type) => {
      const v = k in store ? store[k] : null;
      if (v == null) return type === "json" ? null : v;
      return type === "json" ? JSON.parse(v) : v;
    },
    put: async (k, v) => { store[k] = v; },
    _store: store,
  };
}
const dispatchRuns = () => Array.from({ length: 12 }, (_, i) => {
  const t = Date.now() - (23 - i) * H; // 12 dispatches, 12h..23h old (inside 24h window)
  return mkRun({ event: "workflow_dispatch", conclusion: "failure", created_at: iso(t), updated_at: iso(t) });
});

globalThis.__done = false;
globalThis.__error = null;
globalThis.__result = null;
(async () => {
  const r = {};

  // A: failed run 3 min ago -> failGrace (2 min) passed -> restart
  __runs = [mkRun({ conclusion: "failure", updated_at: iso(Date.now() - 3 * M) })];
  let s = await evaluateRepo(mercy, headers, { WATCHMAN_KV: kvMock([]) });
  r.A_failed3min = { needsRestart: s.needsRestart, throttled: !!s.throttled };

  // A2: failed run 1 min ago -> still inside failGrace
  __runs = [mkRun({ conclusion: "failure", updated_at: iso(Date.now() - 1 * M) })];
  s = await evaluateRepo(mercy, headers, { WATCHMAN_KV: kvMock([]) });
  r.A2_failed1min = { needsRestart: s.needsRestart };

  // B: successful run ended 1 min ago -> inside the 2 min grace, still waiting
  __runs = [mkRun({ conclusion: "success", updated_at: iso(Date.now() - 1 * M) })];
  s = await evaluateRepo(mercy, headers, { WATCHMAN_KV: kvMock([]) });
  r.B_success1min = { needsRestart: s.needsRestart, restarts: s.restarts };

  // B2: successful run ended 3 min ago -> past the 2 min grace -> restart
  __runs = [mkRun({ conclusion: "success", updated_at: iso(Date.now() - 3 * M) })];
  s = await evaluateRepo(mercy, headers, { WATCHMAN_KV: kvMock([]) });
  r.B2_success3min = { needsRestart: s.needsRestart };

  // C: 12 MANUAL dispatch runs in the API, but KV says we only restarted once
  //    -> must NOT be throttled (this is the Oct-7 lockout bug)
  __runs = [mkRun({ updated_at: iso(Date.now() - 25 * M) })].concat(dispatchRuns());
  s = await evaluateRepo(mercy, headers, { WATCHMAN_KV: kvMock([Date.now() - 2 * H]) });
  r.C_manualDispatchesKV = { needsRestart: s.needsRestart, throttled: !!s.throttled, restarts: s.restarts };

  // D: same API payload, no KV binding (fallback) -> old behaviour: throttled
  s = await evaluateRepo(mercy, headers, {});
  r.D_fallbackNoKV = { throttled: !!s.throttled, restarts: s.restarts, resumeAt: s.resumeAt };

  // E: KV has 8 watchman restarts -> throttled, resumeAt = oldest + 24h
  const ts8 = Array.from({ length: 8 }, (_, i) => Date.now() - (8 - i) * H);
  __runs = [mkRun({ updated_at: iso(Date.now() - 25 * M) })];
  s = await evaluateRepo(mercy, headers, { WATCHMAN_KV: kvMock(ts8) });
  r.E_kvThrottle = {
    throttled: !!s.throttled, restarts: s.restarts, resumeAt: s.resumeAt,
    expectedResumeAt: iso(ts8[0] + 24 * H),
  };

  // E2: KV count is pruned on read (entry 30h old no longer counts)
  const old = [Date.now() - 30 * H, Date.now() - 2 * H];
  s = await evaluateRepo(mercy, headers, { WATCHMAN_KV: kvMock(old) });
  r.E2_prune = { restarts: s.restarts, throttled: !!s.throttled };

  // F: active run -> no restart
  __runs = [mkRun({ status: "in_progress", conclusion: null, updated_at: iso(Date.now()), run_started_at: iso(Date.now() - M) })];
  s = await evaluateRepo(mercy, headers, { WATCHMAN_KV: kvMock([]) });
  r.F_active = { needsRestart: s.needsRestart, status: s.status };

  // G: throttle alert fires once, then respects the 30 min cooldown
  globalThis.__posts = 0;
  const baseFetch = globalThis.fetch;
  globalThis.fetch = async function (url, init) {
    if (init && init.method === "POST") globalThis.__posts++;
    return baseFetch(url, init);
  };
  const env = { DISCORD_WEBHOOK_URL: "https://discord.test/webhook" };
  const tstate = {
    name: "mercy-discord-bot",
    reason: "Throttled (12 watchman restarts in 24h)",
    restarts: 12, budget: 8, resumeAt: "2026-10-08T20:00:00.000Z",
  };
  await maybeNotifyThrottled(env, tstate); // 1st alert
  await maybeNotifyThrottled(env, tstate); // inside cooldown -> suppressed
  const realNow = Date.now;
  Date.now = function () { return realNow() + 31 * 60 * 1000; }; // past cooldown
  await maybeNotifyThrottled(env, tstate); // 2nd alert
  Date.now = realNow;
  r.G_throttleAlert = { posts: globalThis.__posts };

  globalThis.__result = r;
})().catch(function (e) {
  globalThis.__error = (e && e.stack) || String(e);
}).finally(function () {
  globalThis.__done = true;
});
"""

ctx = quickjs.Context()
ctx.eval(src)
ctx.eval(TEST)

result = None
POLL = ("globalThis.__done ? (globalThis.__error ? 'ERROR:' + globalThis.__error : "
        "JSON.stringify(globalThis.__result)) : 'PENDING'")
drain_errors = []


def drain(ctx, limit=10000):
    """Run queued microtasks/jobs until the queue is empty."""
    for _ in range(limit):
        try:
            if not ctx.execute_pending_job():
                return
        except Exception as e:
            drain_errors.append(repr(e))
            return


for _ in range(100):
    drain(ctx)
    out = ctx.eval(POLL)
    if out != "PENDING":
        break
    time.sleep(0.05)

if drain_errors:
    print("job queue errors:", drain_errors[:3])

if out == "PENDING":
    print("FAIL: async test never resolved (job queue not drained)")
    sys.exit(1)
if out.startswith("ERROR:"):
    print("FAIL: test threw ->", out[6:])
    sys.exit(1)
result = json.loads(out)

print(json.dumps(result, indent=2))

checks = []
def chk(name, cond):
    checks.append((name, cond))

chk("A: failed 3min -> restart", result["A_failed3min"] == {"needsRestart": True, "throttled": False})
chk("A2: failed 1min -> wait (2m grace)", result["A2_failed1min"]["needsRestart"] is False)
chk("B: success 1min -> wait (2m grace)", result["B_success1min"]["needsRestart"] is False)
chk("B2: success 3min -> restart (2m grace)", result["B2_success3min"]["needsRestart"] is True)
chk("C: 12 manual dispatches + KV(1) -> NOT throttled, restart",
    result["C_manualDispatchesKV"] == {"needsRestart": True, "throttled": False, "restarts": 1})
chk("D: no KV fallback -> throttled at 12", result["D_fallbackNoKV"]["throttled"] is True and result["D_fallbackNoKV"]["restarts"] == 12)
chk("E: KV(8) -> throttled, resumeAt = oldest+24h",
    result["E_kvThrottle"]["throttled"] is True and result["E_kvThrottle"]["resumeAt"] == result["E_kvThrottle"]["expectedResumeAt"])
chk("E2: 30h-old entry pruned", result["E2_prune"] == {"restarts": 1, "throttled": False})
chk("F: active run -> no restart", result["F_active"]["needsRestart"] is False)
chk("G: throttle alert once per 30 min (1 then 2 posts)", result["G_throttleAlert"] == {"posts": 2})

failed = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("PASS  " if ok else "FAIL  ") + n)
print("\n" + ("ALL PASS" if not failed else f"{len(failed)} FAILED"))
sys.exit(0 if not failed else 1)
