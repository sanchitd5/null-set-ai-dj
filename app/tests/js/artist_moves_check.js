// Node check for the batch C artist moves (app/ui/static/artist-moves.js): every planner on synthetic beat grids
// (caps, gates, no-op cases), the deck's vocal-only slip loop (stem hold, the music untouched) and layerPieces booking
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
// the slip loop loops the VOCAL STEM only (the music runs on); no live stems = refused; off-EDM only a whole lyric line
const slip = (o = {}) => am.planSlipLoop(Object.assign({ pos: 55, lineT: 64, bpm: 120, rate: 1, beats: BEATS, exitT: 120,
  genre: "house", slipSupported: true, stemsLive: true, vocals: [[56, 62]], energyNow: 0.4, energyNext: 0.7, count: 0, atBar: 40, lastAtBar: null, quiet: true }, o));
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
  assert.strictEqual(slip({ exitT: 66 }).gate, "exit_guard");
  assert.strictEqual(p.stem, "vocals"); assert.strictEqual(p.whole_line, false);
  // no live stems: refused on any genre, never a full-mix fallback
  for (const genre of ["house", "hip hop", ""]) assert.strictEqual(slip({ stemsLive: false, genre }).gate, "no_stems", genre);
  assert.strictEqual(slip({ stemsLive: undefined }).gate, "no_stems");
  // EDM: the beat-grid loop of the vocal may cut a line; a vocal-free loop would play nothing: refused
  assert.ok(slip({ vocals: [[50, 70]] }).ok, "EDM stutters the vocal");
  assert.strictEqual(slip({ vocals: [[1, 2]] }).gate, "no_vocal");
  assert.strictEqual(slip({ vocals: [[61, 63]] }).gate, "no_vocal", "the sung part is in the second half of the window: the loop is vocal-free");
  assert.strictEqual(slip({ vocals: [] }).gate, "no_vocal");
  // MOVE_GENRE_ALLOW slip_loop_fragment: off-EDM only a WHOLE lyric line (a vocal-active region), repeated once; same cases as dj-core test/move_genre_test.dart
  for (const genre of ["hip hop", "punjabi", "pop", "r&b", "rock", ""]) {
    const w = slip({ genre, vocals: [[56, 58]] });
    assert.ok(w.ok, `${genre}: ${w.reason}`);
    assert.strictEqual(w.whole_line, true); assert.strictEqual(w.stem, "vocals");
    near(w.start, 56); assert.strictEqual(w.loop_beats, 4); near(w.release, 60, 1e-9, "the line plays, then once more");
  }
  for (const v of [[[61, 63]], [[40, 52]], [[56, 56.4]], []]) {   // repeat past the line / a 24 beat stretch / a syllable / no vocal
    const w = slip({ genre: "hip hop", vocals: v });
    assert.strictEqual(w.gate, "genre", JSON.stringify(v)); assert.ok(/slip_loop_fragment/.test(w.reason));
  }
  assert.strictEqual(slip({ genre: "hip hop", pos: 56.5, vocals: [[55.5, 57], [58, 59]] }).start, 58, "a line behind the playhead is skipped");
  assert.ok(slip({ genre: "trance", vocals: [[56, 62]] }).ok);
  // on demand skips only the choice gates
  assert.ok(slip({ onDemand: true, count: 5, lastAtBar: 39, energyNext: 0.1, quiet: false, relaxed: true }).ok);
  assert.strictEqual(slip({ onDemand: true, loopOn: true }).gate, "deck_busy");
  assert.strictEqual(slip({ onDemand: true, stemsLive: false }).gate, "no_stems");
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

// ---------------------------------------------------------------- deck: vocal-only slip loop + layerPieces
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
// a deck in stem mode with live stems (the caller of slipLoop enters stem mode first)
function stemDeck() {
  const d = makeDeck();
  const buf = { duration: 200 };
  d.stems = { drums: buf, bass: buf, vocals: buf, other: buf, lag: 0, ratio: 1 };
  for (const n of ["drums", "bass", "vocals", "other"]) { d.stemGain[n] = node(); d.stemLive[n] = node(); }
  d.stemState = { drums: 1, bass: 1, vocals: 1, other: 1, bus: 0 };
  d._ready = true; Object.defineProperty(d, "stemsReady", { get() { return this._ready; } });
  return d;
}
{
  // only the vocal stem loops, on the grid; the music (drums / bass / other, the deck clock, the loop) is untouched
  ctx.currentTime = 10;
  const d = stemDeck();
  d.play(30);
  ctx.currentTime = 12;                               // song at 32
  near(d._currentPosition(), 32);
  const before = Object.fromEntries(["drums", "bass", "other"].map((n) => [n, [d.stemGain[n].gain.ev.length, d.stemLive[n].gain.ev.length]]));
  const mixEv = d.mixGain.gain.ev.length;
  assert.strictEqual(d.slipLoop(4, 32, 12.1, 16.1), true);
  assert.strictEqual(d.slipLoop(4, 32, 12.1, 16.1), false, "one slip at a time");
  assert.deepStrictEqual(Object.keys(d._holds), ["vocals"], "the vocal stem is the only thing that loops");
  const h = d._holds.vocals.src, bar = 240 / 128;
  assert.strictEqual(h.loop, true); near(h.loopStart, 32); near(h.loopEnd, 32 + bar, 1e-9, "4 beats = one bar of the vocal stem");
  near(h.at, 12.1); near(d._holds.vocals.until, 16.1);
  for (const n of ["drums", "bass", "other"]) {
    assert.deepStrictEqual([d.stemGain[n].gain.ev.length, d.stemLive[n].gain.ev.length], before[n], `${n} stem: no ramp, no hold: it plays straight through`);
    assert.ok(!d._stemSrc[`hold_${n}`]);
  }
  assert.strictEqual(d.mixGain.gain.ev.length, mixEv, "the mix gain is not touched");
  assert.strictEqual(d.loopOn, false, "no deck loop: the song clock is not looped");
  ctx.currentTime = 16;                               // 4 s under the vocal loop: the deck clock ran on, nothing wrapped
  near(d._currentPosition(), 36, 1e-9, "the music position is the deck position");
  near(d.slipPosition(), 36, 1e-9, "the slip shadow is the deck position");
  ctx.currentTime = 18;
  const to = d.slipRelease();
  near(to, 38, 1e-9, "the vocal is back on the song position");
  assert.strictEqual(d._holds.vocals, undefined, "the hold is released");
  assert.strictEqual(d.stemLive.vocals.gain.ev[d.stemLive.vocals.gain.ev.length - 1].v, 1, "the live vocal comes back");
  assert.strictEqual(d.slipPosition(), null); assert.strictEqual(d.slipRelease(), null, "no slip: no-op");
}
{
  // refusals: stopped / reversed / a loop already on / not in stem mode / stems not live / no stems at all: nothing armed
  ctx.currentTime = 10;
  const d = stemDeck();
  assert.strictEqual(d.slipLoop(4, 30, 11, 15), false, "stopped deck: refused");
  d.play(30);
  d.loopOn = true;
  assert.strictEqual(d.slipLoop(4, 30, 11, 15), false, "a loop already on: refused");
  d.loopOn = false; d.reversed = true;
  assert.strictEqual(d.slipLoop(4, 30, 11, 15), false, "reversed: refused");
  d.reversed = false;
  assert.strictEqual(d.slipLoop(4, 30, 15, 11), false, "an empty window: refused");
  const sm = d.stemState; d.stemState = null;
  assert.strictEqual(d.slipLoop(4, 30, 11, 15), false, "full-mix mode: refused (a vocal hold would be inaudible)");
  d.stemState = sm;
  d._ready = false;
  assert.strictEqual(d.slipLoop(4, 30, 11, 15), false, "stems not live: refused, never a full-mix loop");
  assert.deepStrictEqual(Object.keys(d._holds), []); assert.ok(!d._slip); assert.strictEqual(d.loopOn, false);
  const bare = makeDeck(); bare.play(30);
  assert.strictEqual(bare.slipLoop(4, 30, 11, 15), false, "no stems: refused");
  assert.strictEqual(bare.loopOn, false, "and no full-mix loop instead");
}
{
  // someone else takes over: the vocal comes back, no slip is left running
  ctx.currentTime = 10;
  let d = stemDeck(); d.play(30);
  assert.ok(d.slipLoop(8, 30, 11, 15));
  d.toggleLoop();                                     // the listener presses LOOP
  assert.ok(!d._slip); assert.strictEqual(d._holds.vocals, undefined);
  assert.strictEqual(d.slipRelease(), null);
  d = stemDeck(); d.play(30);
  assert.ok(d.slipLoop(8, 30, 11, 15));
  d.seek(80, { user: true });                         // a scrub ends the slip
  assert.ok(!d._slip); assert.strictEqual(d._holds.vocals, undefined); assert.strictEqual(d.loopOn, false);
  d = stemDeck(); d.play(30);
  assert.ok(d.slipLoop(8, 30, 11, 15));
  d.stopNow();
  assert.ok(!d._slip); assert.strictEqual(d._holds.vocals, undefined);
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
    decks, session: { relaxed: false, genre: "house" }, mod: {},
    bus: { emit: (type, detail) => events.push({ type, detail }) },
    log: { step: (kind, o) => steps.push({ kind, o }) },
    ui: { flag: () => true, status: (m) => status.push(m), queryAll: () => [] },
  };
  const advance = (to) => { for (;;) { t.q.sort((a, b) => a.at - b.at); const x = t.q[0]; if (!x || x.at > to) break; t.q.shift(); t.now = x.at; x.fn(); } t.now = to; };
  return { host, events, steps, status, t, advance };
}
function fakeDeck(id, over = {}) {
  const d = { id, playing: true, bpm: 120, loopOn: false, reversed: false, _slices: {}, _holds: {}, stemsReady: false,
    analysis: { beat_times: BEATS, downbeat_times: DOWNBEATS, vocal_active_regions: [[0, 200]], phrase_boundaries_8bar: [0, 16, 32, 48, 64, 80],
      energy_times: Array.from({ length: 100 }, (_, i) => i * 2), energy_curve: Array.from({ length: 100 }, (_, i) => (i >= 32 ? 0.8 : 0.4)) },
    pos: 55, _currentPosition() { return this.pos; }, _playbackRate: () => 1, crossfaderGain: { gain: { value: 1 } },
    slipCalls: [], stemCalls: [], stemsLiveAt() { return !!this.stemsReady; }, stemMix(t, when) { this.stemCalls.push([t, when]); return true; },
    slipLoop(beats, start, at, until) { this.slipCalls.push([beats, start, at, until]); this._slip = { start }; return true; },
    slipPosition() { return 64; }, slipRelease() { this._slip = null; return 64; }, layerPieces: () => ({ ok: true, until: 99 }) };
  return Object.assign(d, over);
}
{
  const a = fakeDeck("a", { stemsReady: true, stems: { vocals: {} } }), b = fakeDeck("b", { playing: false });
  const H = fakeHost({ a, b });
  const created = require(path.join(STATIC, "artist-moves.js"));
  assert.ok(created.planSlipLoop, "core export");
  // mount the runtime the way Engine does: evaluate the file with a global Engine that calls create({host})
  let api = null;
  const sb = { Engine: { mount: (n, create) => { if (n === "artistMoves") api = create({ host: H.host }); } }, console: { info() {}, warn() {} }, Math, Object, Array, Number, JSON, require };
  sb.window = sb;
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(STATIC, "generated", "dj_config.js"), "utf8"), sb);   // index.html order
  vm.runInContext(fs.readFileSync(path.join(STATIC, "drop-line.js"), "utf8"), sb);
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
  assert.strictEqual(ev.detail.stem, "vocals", "the move says it loops the vocal stem");
  H.advance(1.0);                                     // stem mode, then the vocal loop engages on the grid point (audio time 1 .. 9)
  assert.deepStrictEqual(a.slipCalls, [[8, 56, 1, 9]]);
  assert.strictEqual(JSON.stringify(a.stemCalls[0]), "[{},0.99]", "stem mode is entered first");
  H.advance(9.0);                                     // and releases on the line: the vocal is back on the song, the full mix returns
  const rel = H.events.find((e) => e.detail.kind === "artist_move_release");
  assert.ok(rel && rel.detail.line_err_s === 0 && rel.detail.shadow_err_s === 0);
  assert.strictEqual(JSON.stringify(a.stemCalls[1]), "[null,9.03]", "the full mix comes back after the hold");
  // no live stems: the plan is refused, nothing runs, no full-mix loop
  {
    const c = fakeDeck("a"), H2 = fakeHost({ a: c, b: fakeDeck("b", { playing: false }) });
    let api2 = null;
    const sb2 = { Engine: { mount: (n, create) => { if (n === "artistMoves") api2 = create({ host: H2.host }); } }, console: { info() {}, warn() {} }, Math, Object, Array, Number, JSON, require };
    sb2.window = sb2; vm.createContext(sb2);
    for (const f of ["generated/dj_config.js", "drop-line.js", "learned-moves.js", "artist-moves.js"]) vm.runInContext(fs.readFileSync(path.join(STATIC, f), "utf8"), sb2);
    assert.strictEqual(api2.tick(c, o(55)), null);
    assert.ok(H2.steps.some((s) => s.kind === "artist_move_refused" && s.o.decision === "slip_loop" && /no_stems/.test(s.o.why)));
    H2.advance(10);
    assert.deepStrictEqual(c.slipCalls, []); assert.deepStrictEqual(c.stemCalls, []);
  }
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
