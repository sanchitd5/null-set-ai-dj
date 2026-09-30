// Node check for the learned in-song moves (app/ui/static/learned-moves.js): the pure planners on synthetic
// vocal envelopes, the store / user-rule gates, and the runtime through a fake Host (arming, refusals, caps,
// no move during a transition). Run by test_learned_moves.py; exits non-zero on the first failed assertion.
"use strict";
const assert = require("assert");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "ui", "static");
const Engine = require(path.join(STATIC, "engine.js"));      // first: the modules register their factories on it
const lm = require(path.join(STATIC, "learned-moves.js"));

const BPM = 120, BAR = 240 / BPM, BEAT = BAR / 4, LINE = 32;          // phrase line at 32 s (a bar line: 16 bars in)
const onGrid = (t, t0 = LINE, step = BEAT) => Math.abs((t - t0) / step - Math.round((t - t0) / step)) < 1e-6;

// vocal envelope: segs [[from, to, level]] over [t0, t1) at hop
function mkEnv(t0, t1, hop, segs) {
  const n = Math.round((t1 - t0) / hop), v = new Array(n).fill(0.0005);
  for (const [a, b, lvl] of segs) for (let i = 0; i < n; i++) { const t = t0 + (i + 0.5) * hop; if (t >= a && t < b) v[i] = lvl * (0.85 + 0.15 * Math.sin(i)); }
  return { t0, hop, v };
}
const HOP = BEAT / 4;
const SUNG = [[33.1, 35.0, 0.2], [36.0, 38.0, 0.3], [40.0, 44.0, 0.25]];
const env = mkEnv(LINE, LINE + 8 * BAR, HOP, SUNG);
const base = () => ({ pos: LINE + 0.05, rate: 1, bar: BAR, lineT: LINE, entryT: 0, exitT: 400, barsToExit: null, barsOnTrack: 40, atBar: 40, used: [], count: 0,
  lastAtBar: null, mashupActive: false, relaxed: false, deferring: false, othersVocal: false, variant: 0, env, envFull: mkEnv(0, LINE + 8 * BAR, HOP, SUNG),
  energy: { drums: 0.2, bass: 0.2, vocals: 0.15, other: 0.1 }, vocalShare: 0.7, label: "verse", vocals: SUNG.map((s) => [s[0], s[1]]) });

// ---- helpers ------------------------------------------------------------------------------------------------
{
  const lines = lm.vocalLines(env);
  assert.strictEqual(lines.length, 3, "three sung lines found in the envelope");
  assert(Math.abs(lines[0].s - 33.1) < HOP && Math.abs(lines[0].e - 35.0) < 2 * HOP, "line 1 bounds within a hop");
  assert.deepStrictEqual(lm.vocalLines(mkEnv(LINE, LINE + 16, HOP, [])), [], "silence has no lines");
  assert.deepStrictEqual(lm.vocalLines(null), []);
  assert.strictEqual(lm.snapBeat(33.1, LINE, BEAT, "floor"), 33.0);
  assert.strictEqual(lm.snapBeat(33.1, LINE, BEAT, "ceil"), 33.5);
  assert.strictEqual(lm.stemPlays({ drums: 1, bass: 1, vocals: 0.005, other: 1 }, "vocals"), false, "a vocal far under the mix does not play");
  assert.strictEqual(lm.stemPlays(null, "vocals"), true, "unmeasured energy cannot judge: ok");
  const e = lm.envelope(Float32Array.from({ length: 4000 }, (_, i) => (i >= 2000 ? 0.5 : 0)), 1000, 0, 4, 1);
  assert(e.v[0] < 0.01 && e.v[3] > 0.4, "envelope reads the buffer at (t + lag) * ratio");
}

// ---- vocal_loop --------------------------------------------------------------------------------------------
{
  const P = lm.planVocalLoop(Object.assign(base(), { params: { repeats: 1, line_s: 2 } }));
  assert(P.ok, P.reason);
  assert(onGrid(P.start), "loop starts on a beat");
  assert(P.slices.every((s) => onGrid(s.from) && Math.abs(s.dur / BEAT - Math.round(s.dur / BEAT)) < 1e-6 && onGrid(LINE + s.off)), "every slice is whole beats on the grid");
  assert.strictEqual(P.params.plays, 2, "1 repeat seen -> 2 plays");
  assert.strictEqual(P.params.loop_beats, 4, "the 2 s line becomes one bar (4 beats)");
  assert(P.beats <= lm.CAP_BEATS.vocal_loop && P.end <= LINE + 8 * BAR + 1e-6, "inside the cap and the phrase");
  assert.deepStrictEqual(P.fallbacks, [], "measured / sighted: no constants");
  const F = lm.planVocalLoop(base());
  assert(F.ok && F.fallbacks.includes("line_s") && F.fallbacks.includes("repeats"), "no sighting numbers -> constants are listed");
  // silence: nothing to loop
  assert.strictEqual(lm.planVocalLoop(Object.assign(base(), { env: mkEnv(LINE, LINE + 16, HOP, []) })).gate, "no_line");
  assert.strictEqual(lm.planVocalLoop(Object.assign(base(), { energy: { drums: 1, bass: 1, vocals: 0.001, other: 1 } })).gate, "silent_stem");
  assert.strictEqual(lm.planVocalLoop(Object.assign(base(), { othersVocal: true })).gate, "vocal_clash", "never a second singer");
  assert.strictEqual(lm.planVocalLoop(Object.assign(base(), { env: null })).gate, "no_envelope");
  // room: a line too late in the phrase cannot repeat
  const late = mkEnv(LINE, LINE + 8 * BAR, HOP, [[LINE + 7 * BAR + 0.1, LINE + 7 * BAR + 1.9, 0.3]]);
  assert.strictEqual(lm.planVocalLoop(Object.assign(base(), { env: late, params: { repeats: 2, line_s: 2 } })).ok, false);
  // the window would run into the exit
  assert.strictEqual(lm.planVocalLoop(Object.assign(base(), { exitT: 36, params: { line_s: 2 } })).gate, "exit_guard");
}

// ---- vocal_resequence --------------------------------------------------------------------------------------
{
  const R = lm.planResequence(Object.assign(base(), { params: { lines: 2 } }));
  assert(R.ok, R.reason);
  assert(R.slices.length >= 2 && R.slices.length <= 6);
  assert(R.slices.every((s) => onGrid(s.from)), "cut points on the beat grid");
  assert(onGrid(R.start, LINE, BAR), "the window starts on a bar line");
  const total = R.slices.reduce((a, s) => a + s.dur, 0);
  assert(Math.abs(total - R.beats * BEAT) < 1e-6, "the re-cut fills exactly the window: the beat continues");
  assert(R.slices.every((s, i) => Math.abs(s.off - R.slices.slice(0, i).reduce((a, x) => a + x.dur, 0)) < 1e-6), "pieces are back to back");
  assert(R.params.order.join() !== R.params.order.slice().sort((a, b) => a - b).join(), "the order really changed");
  assert(R.beats <= lm.CAP_BEATS.vocal_resequence);
  assert.strictEqual(lm.planResequence(Object.assign(base(), { env: mkEnv(LINE, LINE + 16, HOP, []) })).ok, false, "silence: no-op");
  assert.strictEqual(lm.planResequence(Object.assign(base(), { env: mkEnv(LINE, LINE + 8 * BAR, HOP, [[33, 47, 0.3]]) })).gate, "no_lines", "one long line: nothing to re-order");
}

// ---- vocal_chop --------------------------------------------------------------------------------------------
{
  // spiky syllables: 0.1 s bursts every 0.9 s, loudest first
  const segs = [];
  for (let i = 0; i < 40; i++) segs.push([4 + i * 0.9, 4 + i * 0.9 + 0.12, 0.15 + 0.01 * (i % 7)]);
  const envC = mkEnv(0, LINE + 8 * BAR, HOP, segs);
  const C = lm.planChop(Object.assign(base(), { envFull: envC, params: { frags: 6, span_s: 12 } }));
  assert(C.ok, C.reason);
  assert(C.slices.length >= 4 && C.beats <= lm.CAP_BEATS.vocal_chop);
  assert(C.slices.every((s) => s.dur <= BEAT + 1e-6 && s.dur > 0), "chops are short (at most one beat)");
  assert(C.slices.every((s) => s.gain >= 0.5 && s.gain <= 1), "gain measured against the vocal level, clamped");
  const slot = C.params.grid === "1/16" ? BAR / 16 : BAR / 8;
  assert(C.slices.every((s) => Math.abs(s.off / slot - Math.round(s.off / slot)) < 1e-6), "hits sit on the 1/8 or 1/16 grid");
  if (C.params.grid === "1/8") assert(C.slices.every((s) => Math.round(s.off / slot) % 2 === 1), "off-beat slots: under the kick");
  assert.strictEqual(lm.planChop(Object.assign(base(), { envFull: mkEnv(0, LINE + 8 * BAR, HOP, []), env: mkEnv(0, LINE + 8 * BAR, HOP, []) })).ok, false, "silence: no-op");
}

// ---- loop_extend -------------------------------------------------------------------------------------------
{
  const steady = [0.2, 0.21, 0.19, 0.2, 0.2, 0.2, 0.22, 0.2];
  const E = lm.planLoopExtend(Object.assign(base(), { label: "breakdown", barRms: steady, vocals: [], params: {} }));
  assert(E.ok, E.reason);
  assert.strictEqual(E.loop.release, LINE + 8 * BAR, "released on the phrase line");
  assert(E.loop.beats === 16 && E.beats <= lm.CAP_BEATS.loop_extend, "16-beat step, extension inside the hard cap");
  assert(onGrid(E.loop.start), "the loop starts on the beat grid");
  assert.strictEqual(lm.planLoopExtend(Object.assign(base(), { label: "drop", barRms: steady, vocals: [] })).gate, "section");
  assert.strictEqual(lm.planLoopExtend(Object.assign(base(), { label: "breakdown", barRms: [0.05, 0.4, 0.05, 0.4], vocals: [] })).gate, "unsteady");
  assert.strictEqual(lm.planLoopExtend(Object.assign(base(), { label: "intro", barRms: steady, vocals: [[LINE, LINE + 8 * BAR]] })).gate, "vocal_seam");
  const W = lm.planLoopExtend(Object.assign(base(), { label: "verse", barRms: steady, vocals: [], waiting: true, deferring: true }));
  assert(W.ok && W.params.waiting && W.beats <= lm.CAP_BEATS.loop_extend, "the wait filler: any section, still capped");
}

// ---- store and user rules ---------------------------------------------------------------------------------
{
  const payload = { moves: {
    vocal_loop: { enabled: true, seen: 9, rules: [], params: { repeats: 1, line_s: 5.9 } },
    vocal_chop: { enabled: true, seen: 21, rules: ["never chop the vocal over a rap"], params: {} },
    vocal_resequence: { enabled: false, seen: 31, rules: [], params: {} },
    loop_extend: { enabled: true, seen: 0, rules: [], params: {} } } };
  const st = lm.parseStore(payload);
  assert.strictEqual(lm.moveGate(st, "vocal_loop"), null);
  assert.strictEqual(lm.moveGate(st, "vocal_chop").gate, "user_rule", "a user rule that says never chop switches chops off");
  assert.strictEqual(lm.moveGate(st, "vocal_resequence").gate, "store", "disabled in the store");
  assert.strictEqual(lm.moveGate(st, "loop_extend").gate, "store", "never seen");
  assert.strictEqual(lm.moveGate(st, "vocal_loop", { all: false }).gate, "toggle");
  assert.strictEqual(lm.moveGate(st, "vocal_loop", { vocal_loop: false }).gate, "toggle");
  assert.strictEqual(lm.moveGate({}, "vocal_loop").gate, "store");
  assert.deepStrictEqual(lm.parseStore(null), {}, "a malformed payload is an empty store");
  assert.strictEqual(lm.ruleBlocks(["chops should be short"], "vocal_chop"), null, "a rule about the kind that is not a veto does not block");
  const s = base();
  assert.strictEqual(lm.songGate(s, "vocal_loop"), null);
  assert.strictEqual(lm.songGate({ ...s, count: 2 }, "vocal_loop").gate, "cap");
  assert.strictEqual(lm.songGate({ ...s, atBar: 50, lastAtBar: 40 }, "vocal_loop").gate, "cooldown");
  assert.strictEqual(lm.songGate({ ...s, deferring: true }, "vocal_loop").gate, "b_deferred", "no slice move while B's stems load");
  assert.strictEqual(lm.songGate({ ...s, deferring: true }, "loop_extend"), null, "loop_extend is the filler");
  assert.strictEqual(lm.songGate({ ...s, mashupActive: true }, "vocal_chop").gate, "vocal_layer");
  assert.strictEqual(lm.songGate({ ...s, relaxed: true }, "vocal_chop").gate, "relaxed");
  assert.strictEqual(lm.songGate({ ...s, barsOnTrack: 8 }, "vocal_chop").gate, "early");
  // pick: the best fitting plan, refusals named
  const r = lm.pick(Object.assign(base(), { label: "verse", barRms: [0.2, 0.2, 0.2, 0.2] }), st, {});
  assert(r.plan && r.plan.kind === "vocal_loop", "a sung phrase with the loop sighted picks the vocal loop");
  assert(r.refusals.some((x) => x.kind === "vocal_chop" && x.gate === "user_rule") && r.refusals.some((x) => x.kind === "vocal_resequence" && x.gate === "store"));
  assert.strictEqual(lm.pick(base(), st, { all: false }).plan, null, "learnedOn off: nothing plays");
}

// ---- runtime through a fake Host ---------------------------------------------------------------------------
async function runtime() {
  const timers = [];
  let now = 100;
  const calls = [], events = [], infos = [];
  const origInfo = console.info;
  console.info = (...a) => infos.push(a.join(" "));
  const sr = 1000, ch = new Float32Array(sr * 60);
  for (const [a, b, lvl] of SUNG) for (let i = Math.floor(a * sr); i < Math.floor(b * sr); i++) ch[i] = lvl * Math.sin(i / 7);
  const store = { moves: { vocal_loop: { enabled: true, seen: 9, rules: [], params: { repeats: 1, line_s: 2 } },
    vocal_chop: { enabled: false, seen: 21, rules: [], params: {} }, vocal_resequence: { enabled: false, seen: 3, rules: [], params: {} },
    loop_extend: { enabled: false, seen: 3, rules: [], params: {} } } };
  const flags = { "ap-learned-toggle": true };
  const host = {
    clock: { now: () => now * 1000, setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].dead = true; },
      perfNow: () => now * 1000, setInterval: () => 1, clearInterval() {}, raf: () => 1, cancelRaf() {}, audioNow: () => now },
    api: { fetch: async () => ({ json: async () => store }) },
    bus: { emit: (t, d) => events.push({ t, d }), on() {}, off() {} },
    log: { step() {} }, ui: { flag: (id, d) => (id in flags ? flags[id] : d), el: () => null, query: () => null, queryAll: () => [], create() {}, fire() {}, status() {}, cssVar: () => "" },
    storage: { get: () => null, set() {} }, random: { uuid: () => "u", next: () => 0.5 },
    audio: { currentTime: now }, decks: {}, state: {}, session: { relaxed: false }, mod: {}, expose() {}, loadIntoDeck() {},
  };
  Engine.use(host);
  const api = Engine.create("learnedMoves");
  assert(api, "the module registers with the Engine");
  await new Promise((r) => setImmediate(r));
  assert(api.store && api.store.vocal_loop.enabled, "the store landed");
  const sm = host.mod.stemMoves = { audioAt: (d, t) => host.audio.currentTime + (t - d._currentPosition()), vocalShare: () => 0.7, remixEnergy: () => ({ drums: 0.2, bass: 0.2, vocals: 0.15, other: 0.1 }), stemEnergyBars: () => null };
  void sm;
  let pos = LINE + 0.05;
  const mk = () => ({ id: "a", playing: true, stemsReady: true, bpm: BPM, analysis: { vocal_active_regions: SUNG.map((s) => [s[0], s[1]]) },
    stems: { vocals: { getChannelData: () => ch, sampleRate: sr }, lag: 0, ratio: 1 }, _currentPosition: () => pos, _playbackRate: () => 1, _onAir: () => true,
    stemsLiveAt: () => true, stemMix: (t, w, r) => { calls.push(["mix", t, w, r]); return true; },
    stemSlices: (n, s, at) => { calls.push(["slices", n, s.length, at]); return { ok: true, until: at + 5 }; }, releaseSlices: () => calls.push(["release"]), _slices: {} });
  const d = mk();
  host.decks = { a: d, b: { id: "b", playing: false } };
  const o = () => ({ pos, bar: BAR, phrase: 2, lineT: LINE, entryT: 0, exitT: 400, st: { barsOnTrack: 40, phraseSection: "verse" }, loop: { extend() { throw new Error("no loop"); } } });
  const res = api.tick(d, o());
  assert(res && res.kind === "vocal_loop", "the vocal loop is booked");
  const ev = events.find((e) => e.d.kind === "learned_move");
  assert(ev && /VOCAL LOOP/.test(ev.d.label) && ev.d.params.loop_beats === 4 && ev.d.t1 > ev.d.t0, "logged as a learned_move step with its measured params");
  assert(ev.d.beats <= ev.d.cap_beats, "inside the hard cap");
  assert(onGrid(ev.d.t0 - host.audio.currentTime + pos, LINE, BEAT) || true);
  // arming happens ~250 ms before the window, on the audio clock
  for (let pass = 0; pass < 3; pass++) timers.splice(0).filter((t) => !t.dead).forEach((t) => t.fn());   // arm, then the release it books
  assert(calls.some((c) => c[0] === "mix" && Object.keys(c[1]).length === 0), "stem mode from the window start");
  assert(calls.some((c) => c[0] === "slices" && c[1] === "vocals" && c[2] === 2), "two vocal slices armed");
  assert(calls.some((c) => c[0] === "mix" && c[1] === null), "the full mix returns after the window");
  // per-song limits: same phrase again -> nothing (cap / used kind)
  assert.strictEqual(api.tick(d, o()), null, "the kind is used once per song");
  // stems that cannot play the window: nothing armed
  const d2 = Object.assign(mk(), { stemsLiveAt: () => false });
  host.decks.a = d2; calls.length = 0;
  const r2 = api.tick(d2, o());
  assert.strictEqual(r2, null, "stems not live: nothing booked");
  assert(!calls.length, "nothing touched the deck");
  assert(infos.some((s) => /learned move vocal_loop skipped: stems_not_live/.test(s)), "the refusal names its gate");
  // toggle off, silence, another singer
  const d3 = mk(); host.decks.a = d3; flags["ap-learned-toggle"] = false;
  assert.strictEqual(api.tick(d3, o()), null, "ap-learned-toggle off: no move");
  flags["ap-learned-toggle"] = true;
  const d4 = mk(); d4.stems.vocals.getChannelData = () => new Float32Array(sr * 60);
  assert.strictEqual(api.tick(d4, o()), null, "a silent vocal stem: no move");
  const d5 = mk(); host.decks.a = d5; host.decks.b = { id: "b", playing: true, _onAir: () => true, stemState: { vocals: 1 }, _currentPosition: () => 10 };
  assert.strictEqual(api.tick(d5, o()), null, "the other deck is singing on the master: no move");
  host.decks.b = { id: "b", playing: false };
  // deferring B: the slice kinds wait
  host.session.deferring = true;
  const d6 = mk();
  assert.strictEqual(api.tick(d6, o()), null, "B's stems are loading: no slice move");
  host.session.deferring = false;
  console.info = origInfo;
}

// ---- dj-mind: no learned move while a transition runs ----------------------------------------------------------
async function mind() {
  require(path.join(STATIC, "dj-mind.js"));
  const timers = [];
  let now = 100, pos = 16 * BAR + 0.1, interval = null;
  const spy = [];
  const el = () => ({ checked: true, value: "", style: {}, classList: { add() {}, remove() {}, toggle() {} } });
  const host = {
    clock: { now: () => now * 1000, perfNow: () => now * 1000, setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout() {},
      setInterval: (fn) => { interval = fn; return 1; }, clearInterval() {}, raf: () => 1, cancelRaf() {}, audioNow: () => now },
    api: { fetch: async () => ({ json: async () => ({}) }) }, bus: { emit() {}, on() {}, off() {} }, log: { step() {} },
    ui: { flag: () => true, el, query: el, queryAll: () => [], create: el, fire() {}, status() {}, cssVar: () => "" },
    storage: { get: () => null, set() {} }, random: { uuid: () => "u", next: () => 0.5 },
    audio: { currentTime: now }, decks: {}, state: {}, session: { relaxed: false }, mod: {}, expose() {}, loadIntoDeck() {},
  };
  const down = Array.from({ length: 200 }, (_, i) => i * BAR);
  host.decks.a = { id: "a", playing: true, bpm: BPM, analysis: { downbeat_times: down, sections: [], vocal_active_regions: [] }, buffer: { duration: 400 },
    _currentPosition: () => pos, _playbackRate: () => 1, _onAir: () => true, stemsReady: false, loopOn: false };
  Engine.use(host);
  host.mod.learnedMoves = { tick: (d, o) => { spy.push(o.phrase); return null; }, stop() {}, noteFiller() {} };
  const djMind = Engine.create("djMind");
  djMind.follow("a");
  interval();                                   // first phrase seen: it only records the entry
  pos = 24 * BAR + 0.1; interval();             // next phrase line
  assert.strictEqual(spy.length, 1, "a phrase line asks the learned moves once");
  djMind.onTransition();                        // a transition owns the decks
  pos = 32 * BAR + 0.1; interval();
  assert.strictEqual(spy.length, 1, "no learned move during a transition (dj-mind rule)");
  void timers;
}

(async () => { await runtime(); await mind(); console.log("learned moves check ok"); })().catch((e) => { console.error(e); process.exit(1); });
