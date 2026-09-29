// Node check for the batch B artist moves (research/notes/artist-signature-techniques.md):
//   stem-moves.js   S1 filter_loop (filterLoopPlan), S9 drums_host (drumsWaiver, drumsHostPlan), their run-now entry
//   learned-moves.js S7 vocal_swap (planVocalSwap), S8 acapella_build (planAcapellaBuild), artistPick, their run-now entry
// Synthetic envelopes and stems only. Exits non-zero on the first failed assertion.
"use strict";
const assert = require("assert");
const path = require("path");
const STATIC = path.join(__dirname, "..", "ui", "static");
const Engine = require(path.join(STATIC, "engine.js"));
const sm = require(path.join(STATIC, "stem-moves.js"));
const lm = require(path.join(STATIC, "learned-moves.js"));

const BPM = 120, BAR = 240 / BPM, BEAT = BAR / 4, LINE = 64;
const onGrid = (t, t0 = LINE, step = BEAT) => Math.abs((t - t0) / step - Math.round((t - t0) / step)) < 1e-6;

// ---- S9: the owner's drums-only key waiver ------------------------------------------------------------------
{
  const track = { level: [0.05, 0.08, 0.1, 0.12, 0.15, 0.1, 0.09, 0.11], density: [0.3, 0.4, 0.5, 0.6, 0.5, 0.45, 0.55, 0.5] };
  const hot = { level: [0.2, 0.21, 0.2, 0.22], density: [0.8, 0.8, 0.75, 0.8] };
  const base = { enabled: true, role: "host", win: hot, track, pitchedOn: false, otherOnMaster: false };
  const g = sm.drumsWaiver(base);
  assert(g.granted && /^drums_waiver: granted: /.test(g.text), g.text);
  assert(g.measured.level_margin > 0, "the margin is the track's own spread, measured");
  assert(/^drums_waiver: refused: /.test(sm.drumsWaiver(Object.assign({}, base, { enabled: false })).text), "option off: refused");
  assert(!sm.drumsWaiver(Object.assign({}, base, { pitchedOn: true })).granted, "a pitched stem audible: the key gate stands");
  assert(!sm.drumsWaiver(Object.assign({}, base, { otherOnMaster: true })).granted, "rule 1: the pitched track already on the master");
  assert(!sm.drumsWaiver(Object.assign({}, base, { win: track })).granted, "drums at the track's median only: not energetic");
  assert(!sm.drumsWaiver(Object.assign({}, base, { win: { level: [0.3, 0.3], density: [0.2, 0.2] } })).granted, "loud but sparse: not dense enough");
  assert(!sm.drumsWaiver(Object.assign({}, base, { win: { level: [0.001], density: [0] } })).granted, "silent drums: refused");
  assert(!sm.drumsWaiver(Object.assign({}, base, { win: null })).granted, "unmeasured: refused");
  const enter = Object.assign({}, base, { role: "enter", win: track, otherOnMaster: true });
  assert(sm.drumsWaiver(Object.assign({}, enter, { otherStemSounding: true })).granted, "rule 2: drums over a stem already sounding");
  assert(!sm.drumsWaiver(Object.assign({}, enter, { otherStemSounding: false })).granted, "rule 2 needs a sounding stem");
  // drumsBars: level and 16th density per bar
  const hop = BAR / 16, v = Array.from({ length: 64 }, (_, i) => (i < 32 ? (i % 2 ? 0.2 : 0.02) : 0.2));
  const b = sm.drumsBars({ t0: 0, hop, v }, 0, BAR, 4, 0.1);
  assert.deepStrictEqual(b.density.map((x) => +x.toFixed(2)), [0.5, 0.5, 1, 1], "density = share of hops over the threshold");
  assert(b.level[2] > b.level[0], "level per bar");
  assert(Math.abs(sm.activeThr({ v: [0, 0.001, 0.1, 0.2, 0.3] }) - 0.2) < 1e-9, "threshold: median of the active hops");
}
// drums host plan: nothing pitched from A while B sings, B's vocal only after A's strip, one sub owner
{
  const P = sm.drumsHostPlan(16, 0.7);
  const aStrip = P.events.find((e) => e.deck === "out" && e.bar === 0);
  assert(aStrip.stems.bass === 0 && aStrip.stems.other === 0 && aStrip.stems.vocals === 0 && aStrip.stems.drums > 0, "A drums only from bar 0");
  const bVox = P.events.find((e) => e.deck === "in" && e.stems && e.stems.vocals > 0);
  assert(bVox.bar >= aStrip.bar + aStrip.ramp, "B's vocal rises only after A's pitched stems are gone");
  assert(!P.events.some((e) => e.deck === "out" && e.bar > 0 && e.stems && ["bass", "other", "vocals"].some((n) => e.stems[n] > 0)), "A never brings a pitched stem back");
  const bBass = P.events.find((e) => e.deck === "in" && e.stems && e.stems.bass > 0);
  assert.strictEqual(bBass.bar, 16, "B's bass only on the line (A's bass is off: one sub owner)");
  assert(P.events.every((e) => e.start || e.hold || e.ramp > 0), "ramps, never steps");
}
// ---- S1: filter loop -----------------------------------------------------------------------------------------
{
  const clean = { drums: [0.2, 0.2, 0.21, 0.2], bass: [0.2, 0.2, 0.2, 0.2], vocals: [0.001, 0.002, 0.001, 0.001], other: [0.1, 0.1, 0.1, 0.1] };
  const c = { pA: LINE, barA: BAR, rate: 1, bpmEff: BPM, before: clean, aLeftBars: 30, centroidHz: 1400, bLineBeats: 24, v: 0.7 };
  const P = sm.filterLoopPlan(c);
  assert(P.ok, P.reason);
  assert.strictEqual(P.L, 4, "4 clean bars: a 4-bar loop");
  assert.strictEqual(P.passes, 2, "B's line is longer than the loop: 2 passes");
  assert.strictEqual(P.M, 8, "B's beat on its 8-bar line");
  assert(P.slices.every((s) => onGrid(s.from, LINE, BAR) && Math.abs(s.dur - 4 * BAR) < 1e-9 && onGrid(LINE + s.off, LINE, BAR)), "slices are whole bars on the grid");
  assert.strictEqual(P.slices.length, 3, "passes + 1: the last pass fades under B's drop");
  assert(P.filter.toBar < P.M && P.filter.lowDb <= -26, "A's lows are killed before B's bass enters: one sub owner");
  assert(P.delay.wet <= 0.35 && [BEAT / 2, BEAT / 4].some((x) => Math.abs(x - P.delay.time) < 1e-3), "echo 1/2 or 1/4 beat, wet <= 0.35");
  assert.strictEqual(P.params.hp_hz, 700, "the sweep end comes from A's centroid");
  assert.deepStrictEqual(P.fallbacks, ["delay_wet"], "only the wet is a constant");
  assert(P.events.every((e) => e.start || e.ramp > 0), "ramps, never steps");
  assert(!P.events.some((e) => e.deck === "out" && e.stems && e.stems.vocals > 0), "A never sings under B");
  const F = sm.filterLoopPlan(Object.assign({}, c, { bLineBeats: null, centroidHz: null }));
  assert(F.ok && F.fallbacks.includes("b_line") && F.fallbacks.includes("centroid"), "unmeasured inputs are listed");
  const short = sm.filterLoopPlan(Object.assign({}, c, { bLineBeats: 12 }));
  assert(short.ok && short.passes === 1 && short.M === 4, "a short B line: one pass");
  // the vocal sings in the first 2 bars: a 2-bar loop of the last 2
  const two = sm.filterLoopPlan(Object.assign({}, c, { before: Object.assign({}, clean, { vocals: [0.2, 0.2, 0.001, 0.001] }) }));
  assert(two.ok && two.L === 2 && Math.abs(two.slices[0].from - (LINE - 2 * BAR)) < 1e-9, "2 clean bars: 2-bar loop");
  assert.strictEqual(sm.filterLoopPlan(Object.assign({}, c, { before: Object.assign({}, clean, { vocals: [0.2, 0.2, 0.2, 0.2] }) })).gate, "vocal_in_loop");
  const silent = { drums: [0, 0, 0, 0], bass: [0, 0, 0, 0], vocals: [0, 0, 0, 0], other: [0, 0, 0, 0] };
  assert.strictEqual(sm.filterLoopPlan(Object.assign({}, c, { before: silent })).gate, "vocal_in_loop", "silence: nothing to loop");
  assert.strictEqual(sm.filterLoopPlan(Object.assign({}, c, { before: Object.assign({}, clean, { drums: [0.02, 0.2, 0.2, 0.2], bass: [0.01, 0.2, 0.2, 0.2], other: [0.01, 0.1, 0.1, 0.1] }) })).gate, "unsteady");
  assert.strictEqual(sm.filterLoopPlan(Object.assign({}, c, { aLeftBars: 8 })).gate, "a_ends", "A must run under the loop");
  assert.strictEqual(sm.filterLoopPlan(Object.assign({}, c, { barA: 240 / 50, pA: 100 })).gate, "slice_cap", "hard cap: one slice window under 38 s");
  assert.strictEqual(sm.filterLoopPlan(Object.assign({}, c, { before: null })).gate, "unmeasured");
  // the loudness gates read the plan like the mashup's
  const e = { eOut: { drums: [0.2], bass: [0.2], vocals: [0.001], other: [0.1] }, eIn: { drums: [0.2], bass: [0.2], vocals: [0.2], other: [0.1] } };
  void e;
}

// ---- the variant chooser: two synthetic pairs pick different variants ---------------------------------------
{
  const fits = { drums: () => ({ ok: true, M: 16, why: "energetic drums" }), loop: () => ({ ok: true, M: 8, why: "4-bar loop x2" }) };
  const clash = sm.mashupVariant(Object.assign({ keyOk: false, rap: false, M: 16 }, fits));
  const noRoom = sm.mashupVariant(Object.assign({ keyOk: true, rap: false, M: 0 }, fits));
  const full = sm.mashupVariant(Object.assign({ keyOk: true, rap: false, M: 16 }, fits));
  assert.strictEqual(clash.name, "drums_host", "clashing keys + waiver: drums host");
  assert.strictEqual(noRoom.name, "filter_loop", "no room in A: filter loop");
  assert.strictEqual(full.name, "mashup", "keys agree + room: the full mashup");
  assert.strictEqual(sm.mashupVariant(Object.assign({}, fits, { keyOk: false, rap: false, M: 16, drums: () => ({ ok: false, why: "drums_waiver: refused: x" }) })).name, null, "waiver refused: no mashup");
  let called = false;
  sm.mashupVariant({ keyOk: true, M: 16, drums: () => { called = true; }, loop: () => { called = true; } });
  assert(!called, "the fits are measured only when their case applies");
}
// ---- S7: vocal line swap -------------------------------------------------------------------------------------
function mkEnv(t0, t1, hop, segs) {
  const n = Math.round((t1 - t0) / hop), v = new Array(n).fill(0.0005);
  for (const [a, b, lvl] of segs) for (let i = 0; i < n; i++) { const t = t0 + (i + 0.5) * hop; if (t >= a && t < b) v[i] = lvl * (0.85 + 0.15 * Math.sin(i)); }
  return { t0, hop, v };
}
const HOP = BEAT / 4;
const SUNG = [[LINE + 2.1, LINE + 5.9, 0.2], [LINE + 8.1, LINE + 11.9, 0.25]];
const baseC = () => ({ pos: LINE + 0.05, rate: 1, bar: BAR, lineT: LINE, entryT: 0, exitT: 400, barsToExit: null, barsOnTrack: 40, atBar: 40, used: [], count: 0,
  lastAtBar: null, mashupActive: false, relaxed: false, deferring: false, othersVocal: false, env: mkEnv(LINE, LINE + 8 * BAR, HOP, SUNG),
  envFull: mkEnv(LINE - 16, LINE + 8 * BAR, HOP, SUNG), energy: { drums: 0.2, bass: 0.2, vocals: 0.15, other: 0.1 }, label: "verse", nextLabel: "verse",
  bLines: [{ s: 20.1, e: 23.85, rms: 0.1 }, { s: 30.2, e: 31.0, rms: 0.3 }], bBeat: BEAT, bGrid0: 0, bRate: 1, tempoGap: 0, keyScore: 1, bRap: false, bPlaying: false });
{
  const P = lm.planVocalSwap(baseC());
  assert(P.ok, P.reason);
  assert(onGrid(P.start) && onGrid(P.end) && P.beats >= 4 && P.beats <= 16, "the hole is whole beats on A's grid, 1 to 4 bars");
  assert(P.start <= SUNG[0][0] && P.end >= SUNG[0][1] && P.end <= SUNG[1][0], "the hole holds A's line and only it");
  assert(onGrid(P.guest.from, 0, BEAT) && P.params.fit_err_beats < 0.5, "B's line on its own grid, within half a beat in length");
  assert(P.guest.dur <= P.beats * BEAT + 1e-9, "inside the hole");
  assert(P.guest.gain === 1.2 || P.guest.gain <= 1.2, "gain capped");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { othersVocal: true })).gate, "vocal_clash", "never two singers");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { bPlaying: true })).gate, "b_playing", "no swap while B plays (a transition)");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { keyScore: 0.6 })).gate, "key", "a sung line needs camelot 0.8");
  assert(lm.planVocalSwap(Object.assign(baseC(), { keyScore: 0.6, bRap: true })).ok, "a rap line needs 0.6");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { tempoGap: 0.09 })).gate, "tempo_cap");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { bRate: 1.05 })).gate, "pitch");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { bLines: [{ s: 20.1, e: 21.0, rms: 0.1 }] })).gate, "no_fit", "no line of the same length");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { bLines: null })).gate, "no_b_vocal");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { env: mkEnv(LINE, LINE + 16, HOP, []) })).gate, "no_fit", "silence: no line to swap");
  assert.strictEqual(lm.planVocalSwap(Object.assign(baseC(), { exitT: LINE + 6 })).gate, "exit_guard");
}
// ---- S8: acapella build -------------------------------------------------------------------------------------
{
  const P = lm.planAcapellaBuild(Object.assign(baseC(), { label: "build", nextLabel: "drop" }));
  assert(P.ok, P.reason);
  assert(Math.abs(P.end - (LINE + 8 * BAR)) < 1e-9, "released on the drop line");
  assert(P.beats <= 32 && P.beats % P.params.loop_beats === 0 && P.params.plays >= 2, "whole fragments inside the 32-beat cap");
  assert(onGrid(P.start) && P.slices.every((s) => onGrid(s.from) && onGrid(P.start + s.off)), "on the beat grid");
  assert.strictEqual(lm.planAcapellaBuild(baseC()).gate, "section", "only in a build (section map)");
  assert(lm.planAcapellaBuild(Object.assign(baseC(), { force: true })).ok, "on demand skips the placement");
  assert.strictEqual(lm.planAcapellaBuild(Object.assign(baseC(), { label: "build", othersVocal: true })).gate, "vocal_clash");
  assert.strictEqual(lm.planAcapellaBuild(Object.assign(baseC(), { label: "build", env: mkEnv(LINE, LINE + 16, HOP, []), envFull: mkEnv(LINE, LINE + 16, HOP, []) })).gate, "no_line");
  // artistPick: toggles, song gates, on-demand skips only the rate gates
  assert.strictEqual(lm.artistPick(Object.assign(baseC(), { label: "build" }), { vocal_swap: false, acapella_build: false }).plan, null, "toggles off: nothing");
  const capped = Object.assign(baseC(), { label: "build", count: 5 });
  assert.strictEqual(lm.artistPick(capped, {}).plan, null, "the per-song cap holds");
  assert(lm.artistPick(capped, {}, "acapella_build", true).plan, "on demand skips the cap");
  assert.strictEqual(lm.artistPick(Object.assign(capped, { relaxed: true }), {}, "acapella_build", true).plan, null, "never the relaxed-session gate");
}

// ---- runtime: the run-now entries through a fake Host -------------------------------------------------------
(async () => {
  let now = 100;
  const timers = [], steps = [], infos = [], statuses = [];
  const origInfo = console.info;
  console.info = (...a) => infos.push(a.join(" "));
  const flags = {};
  const listeners = {};
  const host = {
    clock: { now: () => now * 1000, setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].dead = true; },
      perfNow: () => now * 1000, setInterval: () => 1, clearInterval() {}, raf: () => 1, cancelRaf() {}, audioNow: () => now },
    api: { fetch: async () => ({ json: async () => ({ moves: {} }) }) },
    bus: { emit() {}, on: (t, fn) => { listeners[t] = fn; }, off() {} },
    log: { step: (k, o) => steps.push({ k, o }) }, ui: { flag: (id, d) => (id in flags ? flags[id] : d), el: () => null, query: () => null, queryAll: () => [], create() {}, fire() {}, status: (m) => statuses.push(m), cssVar: () => "" },
    storage: { get: () => null, set() {} }, random: { uuid: () => "u", next: () => 0.5 },
    audio: { currentTime: now }, decks: {}, state: {}, session: { relaxed: false }, mod: {}, expose() {}, loadIntoDeck() {},
  };
  Engine.use(host);
  const art = Engine.create("learnedMoves");
  // S8 on demand: refusal when nothing plays, when a transition runs; ok on a valid deck
  assert.strictEqual(art.runNow("acapella_build").ok, false, "nothing playing: refused");
  const sr = 1000, ch = new Float32Array(sr * 120);
  for (const [a, b, lvl] of SUNG) for (let i = Math.floor(a * sr); i < Math.floor(b * sr); i++) ch[i] = lvl * Math.sin(i / 7);
  let pos = LINE + 0.05;
  const calls = [];
  const d = { id: "a", playing: true, stemsReady: true, bpm: BPM, analysis: { downbeat_times: [0], vocal_active_regions: SUNG.map((s) => [s[0], s[1]]) },
    buffer: { duration: 400 }, stems: { vocals: { getChannelData: () => ch, sampleRate: sr }, lag: 0, ratio: 1 }, _currentPosition: () => pos, _playbackRate: () => 1, _onAir: () => true,
    stemsLiveAt: () => true, stemMix: (t) => { calls.push(["mix", t]); return true; },
    stemSlices: (n, s, at) => { calls.push(["slices", n, s.length, at]); return { ok: true, until: at + 5 }; }, releaseSlices() {}, _slices: {} };
  host.decks = { a: d, b: { id: "b", playing: false } };
  host.mod.stemMoves = { audioAt: (x, t) => host.audio.currentTime + (t - x._currentPosition()), vocalShare: () => 0.7, remixEnergy: () => ({ drums: 0.2, bass: 0.2, vocals: 0.15, other: 0.1 }), stemEnergyBars: () => null };
  host.mod.djMind = { transitioning: true };
  const tr = art.onDemand.acapella_build();
  assert(!tr.ok && /transition/.test(tr.why) && statuses.some((s) => /refused - a transition is running/.test(s)), "a transition is running: refused, on the status line");
  host.mod.djMind = { transitioning: false };
  now += 5;                                                          // past the 1 s de-dupe of one click
  const ok = art.onDemand.acapella_build();
  assert(ok.ok, ok.why);
  assert(steps.some((s) => s.k === "artist_move" && s.o.decision === "artist_move: acapella_build"), "step log: artist_move: <id>");
  assert(steps.some((s) => s.k === "artist_run_now" && s.o.decision === "artist_move: acapella_build"), "the on-demand run is logged");
  timers.splice(0).filter((t) => !t.dead).forEach((t) => t.fn());
  assert(calls.some((c) => c[0] === "slices" && c[1] === "vocals"), "the vocal fragment is armed");
  // the djEvents path reaches the same entry (de-duped within the click)
  assert(typeof listeners["ai-action"] === "function", "listens for ai-action on the host bus");
  // the shared learned demand() path also runs an artist kind (on demand: the cap is skipped)
  timers.length = 0;
  const dm = art.demand(d, "acapella_build", { pos, bar: BAR, phrase: 9, lineT: LINE, entryT: 0, exitT: 400, st: {} });
  assert(dm && dm.kind === "acapella_build", JSON.stringify(dm));
  assert(art.demand(d, "vocal_swap", { pos, bar: BAR, phrase: 9, lineT: LINE, entryT: 0, exitT: 400, st: {} }).refused, "demand: named refusal");
  // S7 on demand: B has no stems -> named refusal
  now += 5;
  const sw = art.onDemand.vocal_swap();
  assert(!sw.ok && /no_b_vocal|b_vocal/.test(sw.why), sw.why);
  // stem-moves run-now: named refusals on unsafe states
  const smApi = Engine.create("stemMoves");
  host.decks = { a: Object.assign({}, d, { stemsReady: true }), b: { id: "b", playing: false, buffer: { duration: 300 }, analysis: {}, stems: {}, bpm: BPM } };
  host.mod.djMind = { transitioning: false };
  assert(/vocal entry/.test(smApi.runNow("filter_loop").why), "B's vocal entry unknown: refused");
  host.decks.b._vocalEntry = { entry: 32 };
  host.decks.b.bpm = BPM * 1.1;
  assert(/tempo gap/.test(smApi.runNow("drums_host").why), "tempo cap: refused");
  host.decks.b.bpm = BPM * 1.04;
  assert(/tempo stems/.test(smApi.runNow("filter_loop").why), "a 4 % gap needs key-locked stems: refused");
  host.mod.djMind = { transitioning: true };
  assert(/transition/.test(smApi.runNow("filter_loop").why), "a transition is running: refused");
  console.info = origInfo;
  console.log("artist moves check ok");
})().catch((e) => { console.error(e); process.exit(1); });
