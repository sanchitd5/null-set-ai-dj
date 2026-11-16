// Node check for the lyric remix executor (app/ui/static/lyric-remix.js): the pure helpers (ctx builder, slice split, timing
// math, lead refusal, bookkeeping) and the runtime through a fake Host (no exception on a refusal, arming, donePlans).
// The executor decides nothing: the plan comes from dj-core through the server. Exits non-zero on the first failed assertion.
"use strict";
const assert = require("assert");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "ui", "static");
const Engine = require(path.join(STATIC, "engine.js"));      // first: the modules register their factories on it
const lr = require(path.join(STATIC, "lyric-remix.js"));

const BAR = 2, near = (a, b, e = 1e-9) => Math.abs(a - b) < e;
const input = () => ({ pos: 100, rate: 1.02, bpm: 120, bar: BAR, lineT: 104, exitT: 400, entryT: 8, barsOnTrack: 40, atBar: 48, stemsLive: true,
  vocalBusy: false, drops: [[200, 210]], vocals: [[100, 130]], used: ["x"], count: 1, lastAtBar: 20, variant: 3, donePlans: ["p0"],
  relaxed: true, mashupActive: false, deferring: false, rttS: 0.1 });

// ---- ctx builder ---------------------------------------------------------------------------------------------
{
  const c = lr.songCtx(input());
  assert(near(c.pos, 100 + lr.MIN_LEAD_S * 1.02), "pos advanced by max(min lead, rtt) * rate");
  assert.strictEqual(lr.songCtx(Object.assign(input(), { rttS: 0.8 })).pos, 100 + 0.8 * 1.02, "a slow round trip advances further");
  assert.strictEqual(c.mode, "song"); assert.strictEqual(c.onDemand, false); assert.strictEqual(c.bBpm, null);
  assert.deepStrictEqual(c.used, ["x"]); assert.notStrictEqual(c.used, input().used, "arrays are copies");
  assert.deepStrictEqual(c.donePlans, ["p0"]); assert.strictEqual(c.lastAtBar, 20);
  assert.strictEqual(c.winStart, null); assert.strictEqual(c.guestStems, false);
  const t = lr.transitionCtx(c, { id: "b1", bpm: 124, guestStems: true });
  assert.strictEqual(t.mode, "transition"); assert.strictEqual(t.bBpm, 124); assert.strictEqual(t.guestStems, true);
  assert.strictEqual(t.winStart, c.pos); assert.strictEqual(t.winEnd, 400 - 4 * BAR, "window ends 4 bars before A's exit");
  assert.strictEqual(c.mode, "song", "the song ctx is not mutated");
  assert.strictEqual(lr.transitionCtx(c, null), null, "no B: no transition ctx");
  assert.strictEqual(lr.transitionCtx(c, { id: "b1", bpm: 124, guestStems: false }), null, "B without a vocal stem: none");
  assert.strictEqual(lr.transitionCtx(Object.assign({}, c, { exitT: null }), { id: "b1", bpm: 124, guestStems: true }), null, "no planned exit: none");
  assert.strictEqual(lr.transitionCtx(Object.assign({}, c, { exitT: c.pos + 5 }), { id: "b1", bpm: 124, guestStems: true }), null, "no room before the exit: none");
  assert.deepStrictEqual(lr.body("a1", "", c), { a: "a1", b: null, ctx: c });
  assert.deepStrictEqual(lr.body("a1", "b1", c).b, "b1");
}

// ---- slice split + timing ------------------------------------------------------------------------------------
const plan = { ok: true, kind: "lyric_remix", mode: "transition", start: 110, end: 118, plan_id: "p9", text: "alpha beta", why: "Lyric remix: alpha beta",
  slices: [{ from: 40, dur: 2, off: 0, gain: 1, src: "self" }, { from: 12, dur: 2, off: 2, gain: 0.9, src: "guest", rate: 0.96 },
    { from: 44, dur: 4, off: 4, src: "self" }] };
{
  const s = lr.splitSlices(plan);
  assert.strictEqual(s.self.length, 2); assert.strictEqual(s.guest.length, 1); assert.strictEqual(s.guest[0].from, 12);
  assert.deepStrictEqual(lr.splitSlices(null), { self: [], guest: [] }); assert.deepStrictEqual(lr.splitSlices({ ok: false }), { self: [], guest: [] });
  const g = lr.sliceTiming(plan.slices[1], 50, 1.05, { lag: 0.1, ratio: 2 });
  assert(near(g.t0, 50 + 2 / 1.05) && near(g.len, 2 / 1.05) && near(g.t1, g.t0 + g.len), "start = at + off / rate, length = dur / rate");
  assert(near(g.rate, 0.96 * 1.05), "guest engine rate = slice.rate * deck rate");
  assert(near(g.offset, (12 + 0.1) * 2), "guest start offset = (from + lag) * ratio"); assert.strictEqual(g.gain, 0.9);
  const e = lr.sliceTiming(plan.slices[2], 50, 1, null);
  assert(near(e.t0, 54) && near(e.len, 4) && e.gain === 1 && e.rate === undefined, "a self slice has no rate / offset (stemSlices owns it)");
  const w = lr.windowOf(plan, 50, 1.25);
  assert(near(w.at, 50) && near(w.until, 50 + 8 / 1.25), "window length = (end - start) / rate");
}

// ---- lead refusal + bookkeeping ------------------------------------------------------------------------------
{
  assert.strictEqual(lr.leadRefusal(10.5, 10), null); assert.strictEqual(lr.leadRefusal(10.35, 10), null, "exactly the minimum is allowed");
  const r = lr.leadRefusal(10.2, 10);
  assert.strictEqual(r.gate, "late"); assert(/0\.20 s/.test(r.reason));
  const rec = { used: ["x"], count: 1, lastAtBar: 3 }, done = [];
  lr.noteDone(rec, done, plan, 48); lr.noteDone(rec, done, plan, 56);
  assert.deepStrictEqual(rec.used, ["x", "lyric_remix", "lyric_remix"]); assert.strictEqual(rec.count, 3); assert.strictEqual(rec.lastAtBar, 56);
  assert.deepStrictEqual(done, ["p9"], "a plan id is remembered once");
  assert.strictEqual(lr.statusText(plan), "Lyric remix: alpha beta");
}

// ---- runtime through a fake host -----------------------------------------------------------------------------
// the real Engine.use() asserts the whole Host port; the executor only needs the members the fake has
const mk = (host) => { Engine.host = host; return Engine.create("lyricRemix"); };
function fakeHost(responses) {
  const log = { fetch: [], emit: [], status: [], steps: [], timers: [], slices: 0, mixes: 0 };
  let now = 1000;
  const param = () => ({ value: 0, setValueAtTime() {}, linearRampToValueAtTime() {}, cancelScheduledValues() {} });
  const audio = { currentTime: 10, createBufferSource: () => ({ playbackRate: param(), connect() {}, start() {}, stop() {} }), createBiquadFilter: () => ({ frequency: param(), connect() {} }),
    createGain: () => ({ gain: param(), connect() {}, disconnect() {} }) };
  const deck = (id, extra) => Object.assign({ id, playing: true, bpm: 120, analysis: { bpm: 120 }, stemsReady: true, stemsLiveAt: () => true, stemMix: () => { log.mixes++; return true; }, stemSlices: () => { log.slices++; return { ok: true, until: 20 }; },
    _playbackRate: () => 1, stemGain: { vocals: {} }, stemLive: { vocals: { gain: param() } }, stems: { vocals: { duration: 100 }, lag: 0, ratio: 1 } }, extra);
  const host = {
    audio, state: { trackA: "s1", trackB: "s2" }, session: { relaxed: true }, mod: { stemMoves: { audioAt: (d, t) => audio.currentTime + (t - 100) } },
    clock: { now: () => (now += 50), setTimeout: (f) => { log.timers.push(f); return log.timers.length; }, clearTimeout() {} },
    api: { fetch: (url, opts) => { log.fetch.push([url, opts]); return Promise.resolve({ json: () => Promise.resolve(responses(url, opts)) }); } },
    bus: { emit: (n, d) => log.emit.push([n, d]), on() {} }, ui: { status: (m) => log.status.push(m), flag: () => true }, log: { step: (n, d) => log.steps.push([n, d]) },
  };
  host.decks = { a: deck("a"), b: deck("b") };
  return { host, log };
}
(async () => {
  const origInfo = console.info; const infos = []; console.info = (...a) => infos.push(a.join(" "));
  try {
    // a refusal object: no exception, nothing armed, one quiet info
    {
      const { host, log } = fakeHost((url) => (url.includes("/has") ? { within: 1, pair: 1 } : { ok: false, gate: "no_plan", reason: "none" }));
      const m = mk(host);
      const o = { pos: 100, bar: BAR, phrase: 5, lineT: 100, entryT: 8, exitT: 400, st: {} };
      assert.strictEqual(m.tick(host.decks.a, o), null); await new Promise((r) => setImmediate(r));   // /has lands
      assert.strictEqual(m.tick(host.decks.a, o), null); await new Promise((r) => setImmediate(r)); await new Promise((r) => setImmediate(r));
      assert(log.fetch.some((f) => f[0].startsWith("/api/lyric_remix/has?a=s1&b=")), "asks /has");
      const posts = log.fetch.filter((f) => f[0] === "/api/lyric_remix/plan").map((f) => JSON.parse(f[1].body));
      assert(posts.length >= 2 && posts[0].ctx.mode === "transition" && posts[0].b === "s2" && posts[1].ctx.mode === "song" && posts[1].b === null, "transition first, then song");
      assert.strictEqual(host.decks.a._lyricRemix, undefined, "nothing armed on a refusal"); assert(infos.some((s) => /lyric remix skipped: no_plan/.test(s)));
      assert.strictEqual(log.emit.length, 0); assert.deepStrictEqual(m.donePlans, []);
    }
    // an OK song plan: armed, bookkeeping, ai-activity, status
    {
      const ok = { ok: true, kind: "lyric_remix", mode: "song", start: 101, end: 109, plan_id: "q1", text: "gamma delta", why: "Lyric remix: gamma delta", score: 0.8, beats: 16,
        slices: [{ from: 40, dur: 4, off: 0, gain: 1, src: "self" }, { from: 52, dur: 4, off: 4, gain: 1, src: "self" }] };
      const { host, log } = fakeHost((url) => (url.includes("/has") ? { within: 1, pair: 0 } : ok));
      const m = mk(host);
      const o = { pos: 99, bar: BAR, phrase: 6, lineT: 99, entryT: 8, exitT: 400, st: {} };
      const flush = () => new Promise((r) => setImmediate(r));
      m.tick(host.decks.a, o); await flush(); m.tick(host.decks.a, o); await flush(); await flush();
      const d = host.decks.a;
      assert(d._lyricRemix, "armed"); assert.deepStrictEqual(m.donePlans, ["q1"]); assert.deepStrictEqual(d._learned.used, ["lyric_remix"]); assert.strictEqual(d._learned.count, 1);
      const act = log.emit.find((e) => e[0] === "ai-activity")[1];
      assert.strictEqual(act.label, "LYRIC REMIX"); assert.strictEqual(act.move, "lyric_remix"); assert.strictEqual(act.kind, "learned_move"); assert.strictEqual(act.why, "Lyric remix: gamma delta");
      assert(log.emit.some((e) => e[0] === "vis-moment" && e[1].name === "LYRIC REMIX" && e[1].tier === "accent"));
      assert.deepStrictEqual(log.status, ["Lyric remix: gamma delta"]); assert.strictEqual(log.steps[0][0], "lyric_remix");
      // the next request carries the done plan
      m.tick(host.decks.a, Object.assign({}, o, { phrase: 7 })); await flush(); await flush();
      const last = JSON.parse(log.fetch.filter((f) => f[0] === "/api/lyric_remix/plan").pop()[1].body);
      assert.deepStrictEqual(last.ctx.donePlans, ["q1"]);
      assert.strictEqual(last.ctx.vocalBusy, true, "own window running: vocal busy");
      log.timers[log.timers.length - 1](); assert.strictEqual(log.slices, 1, "the booked timer runs the self slices"); assert.strictEqual(log.mixes, 1, "stem mode entered");
      m.stop(d); assert.strictEqual(d._lyricRemix, null, "stop releases");
    }
    // /has prefetch: the first tick awaits /has once (capped) and plans in the same phrase; prefetch() asks for the booked next song too
    {
      const ok = { ok: true, kind: "lyric_remix", mode: "song", start: 101, end: 109, plan_id: "f1", text: "first", why: "w", slices: [{ from: 40, dur: 4, off: 0, gain: 1, src: "self" }] };
      const { host, log } = fakeHost((url) => (url.includes("/has") ? { within: 1, pair: 0 } : ok));
      const m = mk(host), flush = () => new Promise((r) => setImmediate(r));
      m.prefetch(host.decks.a);
      assert(log.fetch.some((f) => f[0].startsWith("/api/lyric_remix/has?a=s1&b=s2")), "prefetch asks the booked pair");
      assert(log.fetch.some((f) => f[0].startsWith("/api/lyric_remix/has?a=s1&b=") && !f[0].includes("b=s2")), "prefetch asks the song itself");
      m.tick(host.decks.a, { pos: 99, bar: BAR, phrase: 1, lineT: 99, entryT: 8, exitT: 400, st: {} });
      await flush(); await flush(); await flush();
      assert(host.decks.a._lyricRemix, "the FIRST phrase of the song planned and armed (no wait for the next phrase)");
      assert.strictEqual(log.fetch.filter((f) => f[0].startsWith("/api/lyric_remix/has?a=s1&b=s2")).length, 1, "/has is cached per (a,b)");
      // a /has that never answers: awaited once with the cap, then the phrase is skipped without a plan request
      const h2 = fakeHost((url) => (url.includes("/has") ? new Promise(() => {}) : ok));
      h2.host.api.fetch = (url, opts) => { h2.log.fetch.push([url, opts]); return new Promise(() => {}); };
      const m2 = mk(h2.host);
      m2.tick(h2.host.decks.a, { pos: 99, bar: BAR, phrase: 1, lineT: 99, entryT: 8, exitT: 400, st: {} });
      assert.strictEqual(h2.log.timers.length, 1, "one 400 ms cap timer");
      h2.log.timers[0](); await flush(); await flush();
      assert(!h2.log.fetch.some((f) => f[0] === "/api/lyric_remix/plan"), "no plan without /has counts");
      m2.tick(h2.host.decks.a, { pos: 99, bar: BAR, phrase: 2, lineT: 99, entryT: 8, exitT: 400, st: {} });
      assert.strictEqual(h2.log.timers.length, 1, "the wait happens once per pair");
    }
    // overlap ctx (pure) and runtime: overlap tried first, mute_decks restore, replaces_swap hook
    {
      const c = lr.songCtx(input()), b = { id: "b1", bpm: 124, guestStems: true };
      const o = lr.overlapCtx(c, b, { winStart: 90, winEnd: 150, vocalSwap: { start: 120, end: 130 } });
      assert.strictEqual(o.mode, "overlap"); assert.strictEqual(o.guestPlaying, true); assert.strictEqual(o.takeOverSwap, true);
      assert.strictEqual(o.winStart, c.pos, "window never starts before the lead-advanced position"); assert.strictEqual(o.winEnd, 150);
      assert.deepStrictEqual(o.vocalSwap, { start: 120, end: 130 }); assert.strictEqual(lr.overlapCtx(c, b, { winStart: 90, winEnd: 150 }).vocalSwap, null);
      assert.strictEqual(lr.overlapCtx(c, b, null), null); assert.strictEqual(lr.overlapCtx(c, b, { winStart: 90, winEnd: 50 }), null);
      assert.strictEqual(lr.overlapCtx(c, null, { winStart: 90, winEnd: 150 }), null); assert.strictEqual(c.mode, "song");
      const ok = { ok: true, kind: "lyric_remix", mode: "overlap", start: 101, end: 109, plan_id: "o1", text: "ov", why: "w", mute_decks: ["a", "b"], replaces_swap: true,
        slices: [{ from: 40, dur: 4, off: 0, gain: 1, src: "self" }, { from: 12, dur: 4, off: 4, gain: 1, src: "guest", rate: 1 }] };
      const { host, log } = fakeHost((url) => (url.includes("/has") ? { within: 1, pair: 1 } : ok));
      const calls = []; const mkGain = () => { const p = { value: 1, setValueAtTime(v, t) { calls.push(["set", v, t]); }, linearRampToValueAtTime(v, t) { calls.push(["ramp", v, t]); }, cancelScheduledValues() {} }; return p; };
      host.decks.a.stemLive = { vocals: { gain: mkGain() } }; host.decks.b.stemLive = { vocals: { gain: mkGain() } };
      let cancelled = 0; host.mod.stemMoves.cancelVocalSwap = () => { cancelled++; };
      host.mod.djMind = { transitioning: true, overlapInfo: () => ({ winStart: 100, winEnd: 140, vocalSwap: null }) };
      const m = mk(host), flush = () => new Promise((r) => setImmediate(r));
      m.tick(host.decks.a, { pos: 99, bar: BAR, phrase: 3, lineT: 99, entryT: 8, exitT: 400, st: {} }); await flush(); await flush(); await flush();
      const first = JSON.parse(log.fetch.filter((f) => f[0] === "/api/lyric_remix/plan")[0][1].body);
      assert.strictEqual(first.ctx.mode, "overlap", "overlap is tried first while a transition overlap runs"); assert.strictEqual(first.ctx.guestPlaying, true);
      assert(host.decks.a._lyricRemix, "armed");
      log.timers[log.timers.length - 1]();
      assert.strictEqual(cancelled, 1, "replaces_swap calls the host swap cancel");
      const ramps = calls.filter((c) => c[0] === "ramp");
      assert(ramps.some((c) => c[1] === 0) && ramps.some((c) => c[1] === 1), "both decks' live vocal ramp down and back up"); assert(calls.length >= 8, "A and B both muted");
      m.stop(host.decks.a); assert.strictEqual(host.decks.a._lyricRemix, null, "stop releases");
      // no swap cancel hook: only an info line, never a throw
      delete host.mod.stemMoves.cancelVocalSwap;
      assert.doesNotThrow(() => m.arm(host.decks.a, Object.assign({}, ok, { start: 101 })));
      // a mute target without a live vocal stem refuses (quiet), nothing booked
      host.decks.a._lyricRemix = null; host.decks.b.stemLive = null;
      assert.strictEqual(m.arm(host.decks.a, ok).gate, "stems_not_live");
      // no overlap bounds from the host: no overlap ctx, falls to transition
      delete host.mod.djMind.overlapInfo;
      assert.strictEqual(m.arm(host.decks.a, { ok: false }).ok, false);
    }
    // a late plan: refused by the lead check, nothing booked, no bookkeeping
    {
      const { host } = fakeHost(() => ({}));
      const m = mk(host);
      const r = m.arm(host.decks.a, Object.assign({}, plan, { start: 100.1, slices: [plan.slices[0]] }));
      assert.strictEqual(r.ok, false); assert.strictEqual(r.gate, "late"); assert.strictEqual(host.decks.a._lyricRemix, undefined);
      assert.strictEqual(m.arm(host.decks.a, { ok: false, gate: "x" }).ok, false);
      host.decks.b.stems = null;
      assert.strictEqual(m.arm(host.decks.a, plan).gate, "guest_stems", "a guest slice needs B's vocal buffer");
      host.decks.b = null;
      assert.doesNotThrow(() => m.tick(host.decks.a, { pos: 100, bar: BAR, phrase: 1, lineT: 100, st: {} }));
    }
    // a requested remix (wantPlan): pure rules, then the runtime: no /has needed, only its own plan, status while waiting, cleared when armed
    {
      const w = { plan: "r1", a: "s1", b: null }, wp = { plan: "x1", a: "s1", b: "s2" };
      assert.strictEqual(lr.wantPlanFor(w, "s1", null, false), "r1"); assert.strictEqual(lr.wantPlanFor(w, "s9", null, false), null);
      assert.strictEqual(lr.wantPlanFor(w, "s1", "s2", true), null, "a within plan never runs in a transition pass");
      assert.strictEqual(lr.wantPlanFor(wp, "s1", "s2", true), "x1"); assert.strictEqual(lr.wantPlanFor(wp, "s1", "s3", true), null);
      assert.strictEqual(lr.wantPlanFor(wp, "s1", null, false), null); assert.strictEqual(lr.wantPlanFor(null, "s1", null, false), null);
      assert(lr.retryable("stems_not_live") && lr.retryable("wait") && !lr.retryable("lang_mismatch") && !lr.retryable("no_plan"));
      assert.strictEqual(lr.wantStatus("stems_not_live", "x"), "Getting stems for the remix"); assert.strictEqual(lr.wantStatus("wait", "x"), "Remix waiting for its moment");
      assert.strictEqual(lr.wantStatus("lang_mismatch", "two languages"), "Remix not played: two languages");
      const flush = () => new Promise((r) => setImmediate(r));
      const o = { pos: 99, bar: BAR, phrase: 6, lineT: 99, entryT: 8, exitT: 400, st: {} };
      // stems late: refused with a retryable gate, kept, status says so; the next phrase retries and plays
      let stems = false;
      const okPlan = { ok: true, kind: "lyric_remix", mode: "song", start: 101, end: 109, plan_id: "r1", text: "asked", why: "Lyric remix: asked", score: 0.8, beats: 16,
        slices: [{ from: 40, dur: 4, off: 0, gain: 1, src: "self" }] };
      const { host, log } = fakeHost((url) => (url.includes("/has") ? { within: 0, pair: 0 } : stems ? okPlan : { ok: false, gate: "stems_not_live", reason: "not sounding" }));
      const m = mk(host);
      assert.strictEqual(m.request({ plan: "r1", a: "s1", b: null }), true); assert.strictEqual(m.status, "Waiting for the song");
      m.tick(host.decks.a, o); await flush(); await flush(); await flush();
      const post = JSON.parse(log.fetch.filter((f) => f[0] === "/api/lyric_remix/plan")[0][1].body);
      assert.strictEqual(post.ctx.wantPlan, "r1"); assert.strictEqual(post.ctx.mode, "song"); assert.strictEqual(post.b, null);
      assert.strictEqual(log.fetch.filter((f) => f[0] === "/api/lyric_remix/plan").length, 1, "only its own plan runs, no pair or overlap try");
      assert.strictEqual(host.decks.a._lyricRemix, undefined, "stems late: the plain song plays");
      assert.deepStrictEqual(m.want && m.want.plan, "r1", "kept for the next chance"); assert.strictEqual(m.status, "Getting stems for the remix");
      stems = true;
      m.tick(host.decks.a, Object.assign({}, o, { phrase: 7 })); await flush(); await flush(); await flush();
      assert(host.decks.a._lyricRemix, "armed at the retry"); assert.strictEqual(m.want, null); assert.strictEqual(m.status, "");
      // a hard refusal drops the request with the reason; a request for another song is ignored (and dropped once its song was seen leaving)
      const h2 = fakeHost((url) => (url.includes("/has") ? { within: 0, pair: 0 } : { ok: false, gate: "lang_mismatch", reason: "two languages" }));
      const m2 = mk(h2.host);
      m2.request({ plan: "x1", a: "s1", b: "s2" });
      m2.tick(h2.host.decks.a, o); await flush(); await flush(); await flush(); await flush();
      assert.strictEqual(m2.want, null, "dropped"); assert.strictEqual(m2.status, "Remix not played: two languages");
      const h3 = fakeHost(() => ({ within: 0, pair: 0 }));
      const m3 = mk(h3.host);
      m3.request({ plan: "z1", a: "s7", b: null }); m3.tick(h3.host.decks.a, o); await flush(); await flush();
      assert(!h3.log.fetch.some((f) => f[0] === "/api/lyric_remix/plan"), "a request for a song that is not on air asks nothing");
      assert.strictEqual(m3.want.plan, "z1"); assert.strictEqual(m3.wantsDeck(h3.host.decks.a), false);
    }
  } finally { console.info = origInfo; }
  console.log("lyric_remix_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });
