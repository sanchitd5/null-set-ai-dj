// Node check for the LEARNING panel core (app/ui/static/learn-progress.js): what is shown, formatting, poll rate, poller.
const assert = require("assert");
const path = require("path");
const lp = require(path.join(__dirname, "..", "..", "ui", "static", "learn-progress.js"));

const NOW = 1_000_000_000_000;                       // ms
const sec = (ms) => ms / 1000;
const st = (o) => Object.assign({ id: "a", state: "running", stage: "separate", counts: { separate: { done: 2, total: 8 } },
  updated_at: sec(NOW - 1000), started_at: sec(NOW - 600000), techniques_found: {}, warnings: [], error: null, finished_at: null }, o);

// ---- which studies show -----------------------------------------------------------------------------------------------------
assert.strictEqual(lp.isShown(st({}), NOW), true, "running always shows");
assert.strictEqual(lp.isShown(st({ state: "running", updated_at: sec(NOW - 3600e3) }), NOW), true, "running, however quiet");
assert.strictEqual(lp.isShown(st({ state: "done", finished_at: sec(NOW - 9 * 60e3) }), NOW), true, "done 9 min ago");
assert.strictEqual(lp.isShown(st({ state: "done", finished_at: sec(NOW - 11 * 60e3) }), NOW), false, "done 11 min ago: hidden");
assert.strictEqual(lp.isShown(st({ state: "error", finished_at: sec(NOW - 60e3) }), NOW), true);
assert.strictEqual(lp.isShown(st({ state: "stale", updated_at: sec(NOW - 5 * 60e3) }), NOW), true, "stale counts from its last heartbeat");
assert.strictEqual(lp.isShown(st({ state: "stale", updated_at: sec(NOW - 30 * 60e3) }), NOW), false);
assert.strictEqual(lp.isShown(null, NOW), false);

const picked = lp.pick([
  st({ id: "old", state: "done", finished_at: sec(NOW - 60e3), updated_at: sec(NOW - 60e3) }),
  st({ id: "run1", updated_at: sec(NOW - 5e3) }), st({ id: "run2", updated_at: sec(NOW - 1e3) }),
  st({ id: "gone", state: "done", finished_at: sec(NOW - 3600e3), updated_at: sec(NOW - 3600e3) }),
  st({ id: "run3", updated_at: sec(NOW - 9e3) }),
], NOW);
assert.deepStrictEqual(picked.map((s) => s.id), ["run2", "run1", "run3"], "running first, newest first, capped at 3");
assert.deepStrictEqual(lp.pick(undefined, NOW), []);

// ---- formatting -------------------------------------------------------------------------------------------------------------
assert.strictEqual(lp.fmtDuration(45), "45s");
assert.strictEqual(lp.fmtDuration(59.6), "1m");
assert.strictEqual(lp.fmtDuration(600), "10m");
assert.strictEqual(lp.fmtDuration(3900), "1h 05m");
assert.strictEqual(lp.fmtDuration(null), "");
assert.strictEqual(lp.fmtDuration(NaN), "");

assert.strictEqual(lp.view(st({ parts: 3, part: 2 })).part, "part 2/3");
assert.strictEqual(lp.view(st({ parts: 1, part: 1 })).part, "");
let v = lp.view(st({ title: "Big Set", current: "song.mp3", elapsed_s: 780, eta_s: 1800,
  techniques_found: { vocal_loop: 2, stem_intro: 5, bass_swap: 2 }, warnings: ["w1", "w2", "w3", "w4", "w5"] }));
assert.strictEqual(v.title, "Big Set");
assert.strictEqual(v.stage, "separating songs");
assert.strictEqual(v.count, "2/8");
assert.strictEqual(v.pct, 25);
assert.strictEqual(v.current, "song.mp3");
assert.strictEqual(v.elapsed, "13m", "780 s = 13 min");
assert.strictEqual(v.eta, "~30m left");
assert.deepStrictEqual(v.chips, [{ kind: "stem_intro", n: 5 }, { kind: "bass_swap", n: 2 }, { kind: "vocal_loop", n: 2 }], "by count, then name");
assert.deepStrictEqual(v.warnings, ["w3", "w4", "w5"]);
assert.strictEqual(v.warningsMore, 2);

v = lp.view(st({ stage: "detect", counts: { separate: { done: 8, total: 8 } }, id: "zz" }));
assert.strictEqual(v.pct, null, "a stage without a count is indeterminate");
assert.strictEqual(v.title, "zz", "id when there is no title");
assert.strictEqual(v.eta, "");
v = lp.view(st({ state: "done", stage: "done", finished_at: sec(NOW), current: "x", eta_s: 5 }));
assert.strictEqual(v.pct, 100);
assert.strictEqual(v.current, "", "no current track once finished");
v = lp.view(st({ state: "error", stage: "error", error: "ValueError: need a tracklist" }));
assert.strictEqual(v.error, "ValueError: need a tracklist");
assert.strictEqual(lp.view(st({ state: "stale" })).stage, "stopped (no heartbeat)");

// ---- polling rate -----------------------------------------------------------------------------------------------------------
const run = [st({})], idle = [st({ state: "done", finished_at: sec(NOW - 1000) })];
assert.strictEqual(lp.nextDelay(run, NOW, true, false), 5000, "running: 5 s");
assert.strictEqual(lp.nextDelay(idle, NOW, true, false), 30000, "nothing running: 30 s");
assert.strictEqual(lp.nextDelay([], NOW, true, false), 30000);
assert.strictEqual(lp.nextDelay(run, NOW, true, true), 30000, "a failed poll backs off");
assert.strictEqual(lp.nextDelay(run, NOW, false, false), null, "hidden page: no polling");

// ---- the poller over fake deps ----------------------------------------------------------------------------------------------
(async () => {
  let vis = true, calls = 0, timers = [], data = { studies: run }, failNext = false;
  const seen = [];
  const flush = () => new Promise((r) => setImmediate(r));
  const fire = async () => { const h = timers.pop(); h.fn(); await flush(); };     // the pending timer goes off
  const poller = lp.createPoller({
    fetchJson: async (url) => { calls++; assert.strictEqual(url, "/api/learn/progress"); if (failNext) throw new Error("down"); return data; },
    setTimeout: (fn, ms) => { const h = { fn, ms }; timers.push(h); return h; },
    clearTimeout: (h) => { timers = timers.filter((x) => x !== h); },
    now: () => NOW, visible: () => vis, onData: (s) => seen.push(s),
  });
  poller.start(); await flush();
  assert.strictEqual(calls, 1, "start polls at once");
  assert.strictEqual(seen.length, 1);
  assert.deepStrictEqual(timers.map((t) => t.ms), [5000], "busy: next in 5 s");

  poller.kick(); await flush();
  assert.strictEqual(calls, 1, "kick with a timer pending is a no-op");

  data = { studies: idle }; await fire();
  assert.strictEqual(calls, 2);
  assert.deepStrictEqual(timers.map((t) => t.ms), [30000], "nothing running: backs off to 30 s");

  failNext = true; await fire();
  assert.strictEqual(calls, 3);
  assert.strictEqual(seen.length, 2, "no onData on error");
  assert.deepStrictEqual(timers.map((t) => t.ms), [30000], "an error stays at 30 s");

  failNext = false; data = { studies: run }; vis = false; await fire();
  assert.strictEqual(calls, 3, "hidden page: the timer fires but nothing is fetched");
  assert.strictEqual(timers.length, 0, "and nothing is scheduled");
  vis = true; poller.kick(); await flush();
  assert.strictEqual(calls, 4, "visible again: kick polls at once");
  assert.deepStrictEqual(timers.map((t) => t.ms), [5000], "and is back at 5 s");

  poller.stop(); assert.strictEqual(timers.length, 0, "stop clears the timer");
  poller.kick(); await flush();
  assert.strictEqual(calls, 4, "stopped: kick does nothing");
  // ---- glue on a fake host (one DOM-less smoke: mount, render, hide, no throw on a missing panel) ------------------------------
  const node = () => { const n = { children: [], className: "", textContent: "", hidden: false, style: {}, attrs: {},
    classList: { add() {}, remove() {}, toggle() {} },
    append(...c) { n.children.push(...c); }, appendChild(c) { n.children.push(c); return c; }, remove() { n.removed = true; },
    setAttribute(k, v) { n.attrs[k] = v; }, removeAttribute(k) { delete n.attrs[k]; } }; return n; };
  const els = { "learn-panel": node(), "learn-body": node(), "learn-summary": node() };
  let mounted = null;
  globalThis.Engine = { mount: (name, create) => { mounted = { name, create }; } };
  delete require.cache[require.resolve(path.join(__dirname, "..", "..", "ui", "static", "learn-progress.js"))];
  require(path.join(__dirname, "..", "..", "ui", "static", "learn-progress.js"));
  assert.strictEqual(mounted.name, "learnProgress");
  const mkHost = (ui) => ({ ui: { el: (id) => ui[id] || null, create: () => node() }, clock: { now: () => NOW, setTimeout: () => 1, clearTimeout() {} },
    api: { fetch: async () => ({ ok: true, json: async () => ({ studies: [st({ title: "T", current: "c.mp3", techniques_found: { vocal_loop: 1 } })] }) }) } });
  assert.strictEqual(mounted.create({ host: mkHost({}) }), null, "no panel in the page: nothing mounted");
  const api = mounted.create({ host: mkHost(els) });
  await flush(); await flush();
  assert.strictEqual(els["learn-panel"].hidden, false, "a running study shows the panel");
  assert.strictEqual(els["learn-summary"].textContent, "(1 running)");
  assert.strictEqual(els["learn-body"].children.length, 1);
  api.render([]);
  assert.strictEqual(els["learn-panel"].hidden, true, "nothing to show: hidden");
  assert.strictEqual(els["learn-body"].children[0].removed, true);
  delete globalThis.Engine;
  console.log("learn_progress_check ok");
})().catch((e) => { console.error(e); process.exit(1); });
