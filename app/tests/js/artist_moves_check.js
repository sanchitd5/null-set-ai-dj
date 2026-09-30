// Node check for the batch C artist moves (app/ui/static/artist-moves.js): every planner on synthetic beat grids
// (caps, gates, no-op cases), the deck's slip-loop shadow playhead maths and layerPieces booking
// (deck-controller.js), and the runtime / run-now entry through a fake Host port.
//   node app/tests/js/artist_moves_check.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const STATIC = path.join(__dirname, "..", "..", "ui", "static");
const am = require(path.join(STATIC, "artist-moves.js"));

const near = (a, b, e = 1e-6, m = "") => assert.ok(Math.abs(a - b) <= e, `${m} ${a} != ${b}`);
// 120 BPM: beat 0.5 s, bar 2 s, phrase 16 s; beats and downbeats from 0
const BEATS = Array.from({ length: 400 }, (_, i) => i * 0.5);
const DOWNBEATS = Array.from({ length: 100 }, (_, i) => i * 2);

// ---------------------------------------------------------------- S13 slip loop planner
const slip = (o = {}) => am.planSlipLoop(Object.assign({ pos: 55, lineT: 64, bpm: 120, rate: 1, beats: BEATS, exitT: 120,
  slipSupported: true, vocals: [], energyNow: 0.4, energyNext: 0.7, count: 0, atBar: 40, lastAtBar: null, quiet: true }, o));
{
  const p = slip();
  assert.ok(p.ok, JSON.stringify(p));
  assert.strictEqual(p.window_beats, 16); assert.strictEqual(p.loop_beats, 8);
  near(p.start, 56); near(p.release, 64);
  assert.ok(p.window_beats <= am.SLIP_CAP_BEATS, "within the hard cap");
  assert.strictEqual(p.extension_beats, 0, "a slip loop never lengthens the song");
  assert.ok([4, 8, 16].includes(p.loop_beats), "whole-beat loop lengths from the spec set");
  assert.deepStrictEqual(p.fallbacks, []);
  // less room: the 8-beat window (4-beat loop)
  const q = slip({ pos: 57.7 });
  assert.ok(q.ok); assert.strictEqual(q.window_beats, 8); near(q.start, 60);
  assert.strictEqual(slip({ pos: 59.6 }).gate, "late");
  // gates
  assert.strictEqual(slip({ inTransition: true }).gate, "transition");
  assert.strictEqual(slip({ holdActive: true }).gate, "transition");
  assert.strictEqual(slip({ mashupActive: true }).gate, "vocal_layer");
  assert.strictEqual(slip({ slipSupported: false }).gate, "no_slip");
  assert.strictEqual(slip({ loopOn: true }).gate, "deck_busy");
  assert.strictEqual(slip({ busySlices: true }).gate, "deck_busy");
  assert.strictEqual(slip({ relaxed: true }).gate, "relaxed");
  assert.strictEqual(slip({ quiet: false }).gate, "phrase_busy");
  assert.strictEqual(slip({ count: 2 }).gate, "cap");
  assert.strictEqual(slip({ lastAtBar: 20 }).gate, "spacing");
  assert.strictEqual(slip({ energyNext: 0.3 }).gate, "no_build");
  assert.strictEqual(slip({ energyNow: null }).gate, "unmeasured");
  assert.strictEqual(slip({ vocals: null }).gate, "unmeasured");
  assert.strictEqual(slip({ vocals: [[50, 70]] }).gate, "vocal_seam");
  assert.strictEqual(slip({ exitT: 66 }).gate, "exit_guard");
  // on demand skips only the choice gates
  assert.ok(slip({ onDemand: true, count: 5, lastAtBar: 39, energyNext: 0.1, quiet: false, relaxed: true }).ok);
  assert.strictEqual(slip({ onDemand: true, loopOn: true }).gate, "deck_busy");
  assert.strictEqual(slip({ onDemand: true, vocals: [[50, 70]] }).gate, "vocal_seam");
  assert.strictEqual(slip({ onDemand: true, inTransition: true }).gate, "transition");
  // no analysed beats: the bpm grid, listed as a fallback
  const g = slip({ beats: null });
  assert.ok(g.ok); assert.ok(g.fallbacks.includes("beat grid from bpm")); near(g.start, 56);
  // off-grid analysed beat nearest the window start is used (and its error reported)
  const off = slip({ beats: BEATS.map((b) => (b === 56 ? 56.01 : b)) });
  near(off.start, 56.01); near(off.grid_err_s, 0.01, 1e-9);
}

// ---------------------------------------------------------------- S14 cue tease planner
const ENV = { t0: 10, hop: 0.5, v: [0.05, 0.3, 0.02, 0.1, 0.05, 0.3, 0.02, 0.1] };
const tease = (o = {}) => am.planCueTease(Object.assign({ pos: 50, exitT: 64, aBpm: 120, aRate: 1, aBeats: DOWNBEATS, bBpm: 120,
  bPlaying: false, bDrumEnv: ENV, aDrumRms: 0.2 }, o));
{
  const p = tease();
  assert.ok(p.ok, JSON.stringify(p));
  assert.strictEqual(p.stab_beats, 1, "a short hit gets a 1-beat stab");
  assert.strictEqual(p.stabs.length, am.TEASE_MAX_STABS);
  assert.deepStrictEqual(p.stabs.map((s) => s.a_t), [57.5, 59.5, 61.5, 63.5], "beat 4 of each of A's last 4 bars");
  for (const s of p.stabs) {
    near(s.b_from, 10.5, 1e-9, "B's strongest hit");
    assert.ok(s.a_t + s.beats * 0.5 <= 64 + 1e-9, "every stab ends by B's entry");
    near((s.a_t / 0.5) % 1, 0, 1e-9, "on a whole beat");
  }
  near(p.gain, 0.35 * 0.2 / 0.3, 1e-9, "gain measured from A's drums vs B's hit");
  assert.ok(p.hp_hz >= 120, "stabs above the sub owner line");
  near(p.b_rate, 1);
  // a sustained hit: 2-beat stab on beats 2-3
  const sus = tease({ bDrumEnv: { t0: 0, hop: 0.5, v: [0.3, 0.25, 0.05] } });
  assert.strictEqual(sus.stab_beats, 2);
  for (const s of sus.stabs) near(((s.a_t % 2) + 2) % 2, 0.5, 1e-9, "beat 2");
  // measured tempo within the cap, refused beyond
  assert.ok(tease({ bBpm: 126 }).ok);
  assert.strictEqual(tease({ bBpm: 132 }).gate, "tempo");
  // gates
  assert.strictEqual(tease({ inTransition: true }).gate, "transition");
  assert.strictEqual(tease({ mashupActive: true }).gate, "vocal_layer");
  assert.strictEqual(tease({ relaxed: true }).gate, "relaxed");
  assert.ok(tease({ relaxed: true, onDemand: true }).ok);
  assert.strictEqual(tease({ exitT: null }).gate, "no_plan");
  assert.strictEqual(tease({ bPlaying: true }).gate, "b_rolling");
  assert.strictEqual(tease({ bDrumEnv: null }).gate, "no_stems");
  assert.strictEqual(tease({ bDrumEnv: { t0: 0, hop: 0.5, v: [0.001, 0.002] } }).gate, "no_stems");
  assert.strictEqual(tease({ pos: 63 }).gate, "late");
  // fewer bars left: fewer stabs
  assert.strictEqual(tease({ pos: 59 }).stabs.length, 2);
  // unmeasured A level: the constant gain, listed
  const fb = tease({ aDrumRms: null, aBeats: null });
  assert.ok(fb.fallbacks.includes("gain=0.25") && fb.fallbacks.includes("A beat grid from bpm"));
}

// ---------------------------------------------------------------- S12 roll planner
const roll = (o = {}) => am.planRoll(Object.assign({ pos: 60, exitT: 64, bpm: 120, rate: 1, beats: BEATS, aDrumBars: [0.2, 0.2, 0.2, 0.2], fxOk: true }, o));
{
  const p = roll();
  assert.ok(p.ok, JSON.stringify(p));
  assert.strictEqual(p.window_beats, 4); near(p.start, 62); near(p.release, 64);
  assert.strictEqual(p.pieces.length, 8);
  for (const x of p.pieces) { near(x.from, 62); near(x.dur, 0.25); assert.ok(x.a_t + x.dur <= 64 + 1e-9); }
  assert.ok(p.window_beats <= p.cap_beats && p.wet <= 0.3);
  const fill = roll({ aDrumBars: [0.3, 0.02, 0.02, 0.3] });
  assert.strictEqual(fill.window_beats, 2, "uneven drums: a half-bar roll only");
  assert.strictEqual(roll({ pos: 63.5 }).gate, "late");
  assert.strictEqual(roll({ aDrumBars: [0.001, 0.002, 0.001, 0.001] }).gate, "drums_silent");
  assert.strictEqual(roll({ aDrumBars: null }).gate, "no_stems");
  assert.strictEqual(roll({ fxOk: false }).gate, "fx_budget");
  assert.ok(roll({ fxOk: false, onDemand: true }).ok);
  assert.strictEqual(roll({ exitT: null }).gate, "no_plan");
  assert.strictEqual(roll({ inTransition: true }).gate, "transition");
}

// ---------------------------------------------------------------- S11 percussion bridge planner
assert.strictEqual(am.planPercBridge({ deckCount: 2, tempoGapPct: 12 }).gate, "no_third_deck");
assert.strictEqual(am.planPercBridge({ deckCount: 3, tempoGapPct: 2 }).gate, "no_need");
assert.ok(am.planPercBridge({ deckCount: 3, tempoGapPct: 9 }).ok);

// ---------------------------------------------------------------- deck: slip loop shadow playhead + layerPieces
const ctx = { currentTime: 10 };
class Param {
  constructor(v) { this.ev = [{ t: -1, v }]; this._v = v; }
  cancelScheduledValues() {} setValueAtTime(v, t) { this.ev.push({ t, v }); } linearRampToValueAtTime(v, t) { this.ev.push({ t, v, lin: true }); }
  get value() { return this._v; } set value(v) { this._v = v; }
}
const node = () => ({ gain: new Param(1), connect() {}, disconnect() {} });
class Src { constructor() { this.playbackRate = new Param(1); } connect() {} disconnect() {} start(at, off) { this.at = at; this.off = off; } stop(t) { this.stopped = t; } }
Object.assign(ctx, { state: "running", createGain: node, createBufferSource: () => new Src(),
  createBiquadFilter: () => ({ type: "lowpass", frequency: new Param(350), connect() {}, disconnect() {} }) });
const srcText = fs.readFileSync(path.join(STATIC, "deck-controller.js"), "utf8");
const i0 = srcText.indexOf("class Deck {"), i1 = srcText.indexOf("\n}\n", i0);
const timeouts = [];
const sandbox = { audioCtx: ctx, STEM_NAMES: ["drums", "bass", "vocals", "other"], SPIN_UP_SECONDS: 0.5, SPIN_DOWN_SECONDS: 0.8, BRAKE_SECONDS: 1,
  window: { dispatchEvent() {} }, CustomEvent: class {}, state: {}, setTimeout: (f) => { timeouts.push(f); return 0; }, clearTimeout() {}, Math, console };
vm.createContext(sandbox);
vm.runInContext(srcText.slice(i0, i1 + 2) + "\nthis.Deck = Deck;", sandbox);
function makeDeck() {
  const d = Object.create(sandbox.Deck.prototype);
  Object.assign(d, { id: "a", buffer: { duration: 200 }, source: null, playing: false, startedAt: 0, startOffset: 0, loopOn: false, loopBeats: 4,
    bpm: 128, trimStart: 0, trimEnd: 200, reversed: false, _pitchPercent: 0, _bendPercent: 0, _braking: false, _spinningUp: false,
    _holds: {}, _slices: {}, _stemSrc: {}, stems: null, mixGain: node(), inputGain: node(), vocalBusGain: node(), stemGain: {}, stemLive: {}, stemMeter: {},
    crossfaderGain: node(), onPlayStateChange: null });
  d._syncWavesurferCursor = () => {}; d._ensureReverseBuffer = () => {}; d._activeBuffer = () => d.buffer;
  return d;
}
{
  ctx.currentTime = 10;
  const d = makeDeck();
  d.play(30);
  ctx.currentTime = 12;                               // song at 32
  near(d._currentPosition(), 32);
  assert.strictEqual(d.slipLoop(4, 32.5), false, "a start 0.5 s off the playhead is late: refused");
  assert.strictEqual(d.slipLoop(4, 32), true);
  assert.strictEqual(d.slipLoop(4, 32), false, "one slip at a time");
  assert.ok(d.loopOn); assert.strictEqual(d.loopBeats, 4);
  const L = 4 * 60 / 128;                             // 1.875 s loop
  ctx.currentTime = 16;                               // 4 s under the loop
  near(d._currentPosition(), 32 + (4 % L), 1e-9, "the heard position wraps in the loop");
  near(d.slipPosition(), 36, 1e-9, "the shadow runs on");
  // a pitch change mid-slip: the shadow is rebased with the deck clock
  d.setPitchPercent(5);
  ctx.currentTime = 18;
  near(d.slipPosition(), 36 + 2 * 1.05, 1e-9, "shadow at the new rate after the change");
  // a glide mid-slip: the shadow integrates it like the deck clock
  d.setPitchPercent(0);
  d.rampPitchPercent(4, 2);                          // 1.0 -> 1.04 over 2 s from t=18
  ctx.currentTime = 20;
  near(d.slipPosition(), 38.1 + 2 * 1.02, 1e-9, "shadow over a linear glide");
  const to = d.slipRelease();
  near(to, 40.14, 1e-9, "release lands on the shadow");
  near(d.startOffset, 40.14, 1e-9, "the deck continues from there");
  assert.strictEqual(d.loopOn, false); assert.strictEqual(d.loopBeats, 4, "loop length restored");
  assert.strictEqual(d.slipPosition(), null); assert.strictEqual(d.slipRelease(), null, "no slip: no-op");
}
{
  ctx.currentTime = 10;
  const d = makeDeck();
  assert.strictEqual(d.slipLoop(4), false, "stopped deck: refused");
  d.play(30);
  d.loopOn = true;
  assert.strictEqual(d.slipLoop(4), false, "a loop already on: refused");
  d.loopOn = false; d.reversed = true;
  assert.strictEqual(d.slipLoop(4), false, "reversed: refused");
  d.reversed = false;
  assert.ok(d.slipLoop(8));
  d.toggleLoop();                                     // the listener takes the loop off
  assert.strictEqual(d._slip, null); assert.strictEqual(d.loopOn, false);
  assert.strictEqual(d.slipRelease(), null);
  assert.ok(d.slipLoop(8));
  d.seek(80, { user: true });                         // a scrub ends the slip, no loop left running
  assert.strictEqual(d._slip, null); assert.strictEqual(d.loopOn, false); assert.strictEqual(d.loopBeats, 4);
  assert.ok(d.slipLoop(8));
  d.stopNow();
  assert.strictEqual(d._slip, null); assert.strictEqual(d.loopOn, false);
}
{
  ctx.currentTime = 10;
  const d = makeDeck();
  const buf = { duration: 100 };
  assert.strictEqual(d.layerPieces(buf, [{ from: 1, dur: 0.5, at: 11 }]), false, "stopped deck: nothing armed");
  d.play(30);
  assert.strictEqual(d.layerPieces(buf, [{ from: 1, dur: 0.5, at: 9 }]), false, "a piece in the past: nothing armed");
  assert.strictEqual(d.layerPieces(buf, []), false);
  const r = d.layerPieces(buf, [{ from: 1, dur: 0.5, at: 11 }, { from: 1, dur: 0.5, at: 12 }], { rate: 1.02, gain: 0.3, hpHz: 60 });
  assert.ok(r.ok); near(r.until, 12 + 0.5 / 1.02, 1e-9);
  const rec = d._layers[0];
  assert.ok(rec.hp.type === "highpass" && rec.hp.frequency.value >= 120, "high-pass never below the 120 Hz owner line");
  assert.strictEqual(rec.srcs.length, 2);
  near(rec.srcs[0].playbackRate.value, 1.02);
  d.stopNow();
  assert.strictEqual(d._layers.length, 0, "a stopped deck stops its layers");
  assert.ok(rec.srcs.every((s) => s.stopped != null));
}

// ---------------------------------------------------------------- runtime + run-now through a fake Host port
function fakeHost(decks) {
  const t = { now: 0, q: [] };
  const events = [], steps = [], status = [];
  const host = {
    audio: { get currentTime() { return t.now; } },
    clock: { now: () => t.now * 1000, perfNow: () => t.now * 1000, setTimeout: (fn, ms) => { t.q.push({ at: t.now + ms / 1000, fn }); return t.q.length; }, clearTimeout() {} },
    decks, session: { relaxed: false }, mod: {},
    bus: { emit: (type, detail) => events.push({ type, detail }) },
    log: { step: (kind, o) => steps.push({ kind, o }) },
    ui: { flag: () => true, status: (m) => status.push(m), queryAll: () => [] },
  };
  const advance = (to) => { for (;;) { t.q.sort((a, b) => a.at - b.at); const x = t.q[0]; if (!x || x.at > to) break; t.q.shift(); t.now = x.at; x.fn(); } t.now = to; };
  return { host, events, steps, status, t, advance };
}
function fakeDeck(id, over = {}) {
  const d = { id, playing: true, bpm: 120, loopOn: false, reversed: false, _slices: {}, _holds: {}, stemsReady: false,
    analysis: { beat_times: BEATS, downbeat_times: DOWNBEATS, vocal_active_regions: [], phrase_boundaries_8bar: [0, 16, 32, 48, 64, 80],
      energy_times: Array.from({ length: 100 }, (_, i) => i * 2), energy_curve: Array.from({ length: 100 }, (_, i) => (i >= 32 ? 0.8 : 0.4)) },
    pos: 55, _currentPosition() { return this.pos; }, _playbackRate: () => 1, crossfaderGain: { gain: { value: 1 } },
    slipCalls: [], slipLoop(beats, start) { this.slipCalls.push([beats, start]); this._slip = { start }; return true; },
    slipPosition() { return 64; }, slipRelease() { this._slip = null; return 64; }, layerPieces: () => ({ ok: true, until: 99 }) };
  return Object.assign(d, over);
}
{
  const a = fakeDeck("a"), b = fakeDeck("b", { playing: false });
  const H = fakeHost({ a, b });
  const created = require(path.join(STATIC, "artist-moves.js"));
  assert.ok(created.planSlipLoop, "core export");
  // mount the runtime the way Engine does: evaluate the file with a global Engine that calls create({host})
  let api = null;
  const sb = { Engine: { mount: (n, create) => { if (n === "artistMoves") api = create({ host: H.host }); } }, console: { info() {}, warn() {} }, Math, Object, Array, Number, JSON, require };
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(STATIC, "learned-moves.js"), "utf8"), sb);
  vm.runInContext(fs.readFileSync(path.join(STATIC, "artist-moves.js"), "utf8"), sb);
  assert.ok(api && api.tick && api.runNow);
  const o = (pos) => ({ pos, bar: 2, entryT: 0, lineT: 64, exitT: 120, quiet: true, holdActive: false, mashupActive: false, fxOk: true });
  // too far from the line: nothing planned yet
  a.pos = 50;
  assert.strictEqual(api.tick(a, o(50)), null);
  a.pos = 55;
  const res = api.tick(a, o(55));
  assert.ok(res && res.busyS > 0, "slip booked, the deck is spoken for");
  near(res.busyS, 9.1, 1e-9);
  const ev = H.events.find((e) => e.detail.kind === "artist_move");
  assert.strictEqual(ev.detail.label, "artist_move: slip_loop");
  assert.strictEqual(ev.detail.cap_beats, 16);
  assert.strictEqual(api.tick(a, o(55.2)), null, "one plan per phrase line");
  H.advance(1.0);                                     // the loop engages on the grid point
  assert.deepStrictEqual(a.slipCalls, [[8, 56]]);
  H.advance(9.0);                                     // and releases on the line, onto the shadow
  const rel = H.events.find((e) => e.detail.kind === "artist_move_release");
  assert.ok(rel && rel.detail.line_err_s === 0 && rel.detail.shadow_err_s === 0);
  // a refusal is said once per phrase
  const n0 = H.steps.filter((s) => s.kind === "artist_move_refused").length;
  const busy = Object.assign(o(55), { lineT: 80, quiet: false });
  api.tick(a, Object.assign({}, busy, { pos: 71 })); api.tick(a, Object.assign({}, busy, { pos: 71.5 }));
  const refused = H.steps.filter((s) => s.kind === "artist_move_refused").slice(n0);
  assert.ok(refused.some((s) => /phrase_busy/.test(s.o.why)));
  assert.strictEqual(refused.filter((s) => s.o.decision === "slip_loop").length, 1);
  assert.ok(H.steps.some((s) => s.kind === "artist_move_refused" && s.o.decision === "perc_bridge" && /no_third_deck/.test(s.o.why)));

  // run-now: refusals on unsafe states, ok on a valid one; each run logged and shown
  a.pos = 20; a._slip = null;
  assert.strictEqual(api.runNow("nope").ok, false);
  a.loopOn = true;
  let r = api.runNow("slip_loop");
  assert.strictEqual(r.ok, false); assert.ok(/deck_busy/.test(r.why));
  a.loopOn = false;
  H.host.mod.djMind = { fireAt: () => null, busy: () => true };
  r = api.runNow("slip_loop");
  assert.strictEqual(r.ok, false); assert.ok(/transition/.test(r.why), "a transition owns the deck: refused");
  H.host.mod.djMind = { fireAt: () => null, busy: () => false };
  r = api.runNow("slip_loop");
  assert.ok(r.ok, r.why);
  assert.ok(H.status.some((m) => /^SLIP LOOP: /.test(m)));
  assert.ok(H.steps.some((s) => s.kind === "artist_move_now" && s.o.decision === "slip_loop"));
  r = api.runNow("perc_bridge");
  assert.strictEqual(r.ok, false); assert.ok(/no_third_deck/.test(r.why));
  r = api.runNow("cue_tease");
  assert.strictEqual(r.ok, false); assert.ok(/no_stems/.test(r.why), "no stems on B: nothing armed");
  a.playing = false;
  r = api.runNow("roll");
  assert.strictEqual(r.ok, false); assert.ok(/nothing is playing/.test(r.why));
}

console.log("artist_moves_check ok");
